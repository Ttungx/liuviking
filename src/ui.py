"""PySide6 主界面：连接控制 + WASD 遥控 + 调试日志。

安全要求（交接文档第 5/8 节）全部在此落实：

- W/S/A/D 驱动，Space 急停，Esc 停止并断开；
- 窗口失焦 STOP（可配置）；程序退出 best-effort STOP；
- 多键状态机：最后按下的方向键优先，未全部松开不平白发送 STOP；
- 连接/重连后默认 STOP，绝不恢复上一次运动；
- socket 异常显示错误并清空按键状态，重连前不再接受运动指令。
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src import protocol
from src.config import load_config, save_config
from src.keyboard_controller import DRIVING_KEYS, KeyStateMachine, MotionRefreshWatchdog
from src.robot_client import ConnectionState, RobotClient
from src.video_client import DEFAULT_PATH, MJPEGReader

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
_LOG_DATEFMT = "%H:%M:%S"

_STATE_STYLE = {
    ConnectionState.DISCONNECTED: ("#9e9e9e", "未连接"),
    ConnectionState.CONNECTING: ("#ffb300", "连接中…"),
    ConnectionState.CONNECTED: ("#43a047", "已连接"),
    ConnectionState.ERROR: ("#e53935", "连接错误"),
}

_DIRECTION_TEXT = {
    None: "STOP",
    "w": "前进 W",
    "s": "后退 S",
    "a": "左转 A",
    "d": "右转 D",
}

_INACTIVE_DIRECTION_STYLE = "background-color:#f5f5f5; color:#616161; border-radius:12px;"
_ACTIVE_DIRECTION_STYLE = "background-color:#e8f5e9; color:#2e7d32; border-radius:12px;"


class ClientBridge(QObject):
    """把 RobotClient / MJPEGReader 工作线程的回调封送到 Qt 主线程。"""

    status_received = Signal(str, str)
    log_received = Signal(str)
    video_frame = Signal(bytes)
    video_status = Signal(str)


class QtLogHandler(logging.Handler):
    """把 logging 记录转发到界面日志窗口（跨线程 Signal 安全）。"""

    def __init__(self, bridge: ClientBridge) -> None:
        super().__init__()
        self._bridge = bridge
        self.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self._bridge.log_received.emit(message)
        except Exception:  # pragma: no cover - 日志永远不能拖垮程序
            self.handleError(record)


_LOGGING_READY = False


def setup_logging(bridge: ClientBridge, level: int = logging.INFO) -> None:
    """配置根 logger：控制台 + 界面日志（进程内只配置一次）。"""
    global _LOGGING_READY
    if _LOGGING_READY:
        return
    root = logging.getLogger()
    root.setLevel(level)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    root.addHandler(console)
    root.addHandler(QtLogHandler(bridge))
    _LOGGING_READY = True


class MainWindow(QWidget):
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        connect_on_start: bool = False,
        log_level: int = logging.INFO,
        video_url: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.config = load_config()
        if host:
            self.config["host"] = str(host)
        if port:
            self.config["port"] = int(port)

        self.machine = KeyStateMachine()
        self.motion_watchdog = MotionRefreshWatchdog()
        self.bridge = ClientBridge()
        setup_logging(self.bridge, log_level)

        self.client = RobotClient(status_callback=self._status_callback)
        self.client.swap_left_right = bool(self.config["swap_left_right"])
        self._warned_disconnected = False

        self._video: Optional[MJPEGReader] = None
        self._video_url_override = video_url
        self._last_video_render = 0.0
        self._first_video_frame = True

        self._build_ui()
        self._connect_signals()
        self._apply_config_to_ui()

        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(1000)

        self._motion_watchdog_timer = QTimer(self)
        self._motion_watchdog_timer.timeout.connect(self._check_motion_watchdog)
        self._motion_watchdog_timer.start(300)

        self.setWindowTitle("小R科技 WiFi 小车 WASD 遥控器")
        self.resize(980, 780)
        self.setFocusPolicy(Qt.StrongFocus)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        if self._video_url_override:
            self.video_checkbox.setChecked(True)

        if connect_on_start:
            QTimer.singleShot(200, self._toggle_connection)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # --- 连接栏 ---
        self.host_input = QLineEdit()
        self.host_input.setMaximumWidth(190)
        self.host_input.setPlaceholderText("192.168.88.100")

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(2001)
        self.port_spin.setMaximumWidth(100)

        self.connect_button = QPushButton("连接")
        self.connect_button.setFocusPolicy(Qt.NoFocus)
        self.connect_button.setMinimumWidth(110)

        self.status_dot = QLabel()
        self.status_dot.setFixedSize(14, 14)
        self.status_dot.setStyleSheet("background-color:#9e9e9e; border-radius:7px;")
        self.status_label = QLabel("未连接")

        self.stats_label = QLabel("已发送 0 帧")
        self.stats_label.setStyleSheet("color:#757575;")

        conn_layout = QHBoxLayout()
        conn_layout.addWidget(QLabel("IP"))
        conn_layout.addWidget(self.host_input)
        conn_layout.addWidget(QLabel("端口"))
        conn_layout.addWidget(self.port_spin)
        conn_layout.addWidget(self.connect_button)
        conn_layout.addSpacing(12)
        conn_layout.addWidget(self.status_dot)
        conn_layout.addWidget(self.status_label)
        conn_layout.addStretch(1)
        conn_layout.addWidget(self.stats_label)
        conn_box = QGroupBox("连接")
        conn_box.setLayout(conn_layout)

        # --- 方向显示 + 急停 ---
        self.direction_label = QLabel("STOP")
        self.direction_label.setAlignment(Qt.AlignCenter)
        self.direction_label.setMinimumHeight(120)
        self.direction_label.setStyleSheet(_INACTIVE_DIRECTION_STYLE)
        direction_font = self.direction_label.font()
        direction_font.setPointSize(30)
        direction_font.setBold(True)
        self.direction_label.setFont(direction_font)

        self.estop_button = QPushButton("紧急停车 (Space)")
        self.estop_button.setFocusPolicy(Qt.NoFocus)
        self.estop_button.setMinimumHeight(60)
        self.estop_button.setStyleSheet(
            "QPushButton{background-color:#e53935;color:white;font-size:16px;"
            "font-weight:bold;border-radius:8px;}"
            "QPushButton:pressed{background-color:#b71c1c;}"
        )

        drive_layout = QVBoxLayout()
        drive_layout.addWidget(self.direction_label)
        drive_layout.addWidget(self.estop_button)
        drive_box = QGroupBox("运动 (W/S/A/D，松开即停)")
        drive_box.setLayout(drive_layout)

        # --- 速度与选项 ---
        self.left_speed_spin = QSpinBox()
        self.left_speed_spin.setRange(0, 100)
        self.left_speed_spin.setSuffix(" %")
        self.right_speed_spin = QSpinBox()
        self.right_speed_spin.setRange(0, 100)
        self.right_speed_spin.setSuffix(" %")
        self.apply_speed_button = QPushButton("应用速度")
        self.apply_speed_button.setFocusPolicy(Qt.NoFocus)

        self.swap_checkbox = QCheckBox("左右互换校准")
        self.swap_checkbox.setToolTip("实车测试时若左右相反，勾选后在客户端修正，不改 GPIO")
        self.focus_stop_checkbox = QCheckBox("窗口失焦自动急停")
        self.focus_stop_checkbox.setChecked(True)

        speed_layout = QHBoxLayout()
        speed_layout.addWidget(QLabel("左侧速度"))
        speed_layout.addWidget(self.left_speed_spin)
        speed_layout.addWidget(QLabel("右侧速度"))
        speed_layout.addWidget(self.right_speed_spin)
        speed_layout.addWidget(self.apply_speed_button)
        speed_layout.addSpacing(16)
        speed_layout.addWidget(self.swap_checkbox)
        speed_layout.addWidget(self.focus_stop_checkbox)
        speed_layout.addStretch(1)
        speed_box = QGroupBox("速度 / 校准（原厂源码已确认支持 0-100%）")
        speed_box.setLayout(speed_layout)

        # --- 云台 / 车灯 ---
        self.servo_combo = QComboBox()
        self.servo_combo.addItems([str(i) for i in range(1, 9)])
        self.servo_combo.setMaximumWidth(60)
        self.servo_slider = QSlider(Qt.Horizontal)
        self.servo_slider.setRange(protocol.SERVO_MIN_ANGLE, protocol.SERVO_MAX_ANGLE)
        self.servo_slider.setValue(90)
        self.servo_value_label = QLabel("90°")
        self.servo_value_label.setMinimumWidth(40)
        self.servo_apply_button = QPushButton("应用")
        self.servo_apply_button.setFocusPolicy(Qt.NoFocus)
        self.servo_center_button = QPushButton("复位 90°")
        self.servo_center_button.setFocusPolicy(Qt.NoFocus)
        self.light_on_button = QPushButton("开灯")
        self.light_on_button.setFocusPolicy(Qt.NoFocus)
        self.light_off_button = QPushButton("关灯")
        self.light_off_button.setFocusPolicy(Qt.NoFocus)

        accessory_layout = QHBoxLayout()
        accessory_layout.addWidget(QLabel("舵机号"))
        accessory_layout.addWidget(self.servo_combo)
        accessory_layout.addWidget(self.servo_slider, 1)
        accessory_layout.addWidget(self.servo_value_label)
        accessory_layout.addWidget(self.servo_apply_button)
        accessory_layout.addWidget(self.servo_center_button)
        accessory_layout.addSpacing(12)
        accessory_layout.addWidget(self.light_on_button)
        accessory_layout.addWidget(self.light_off_button)
        accessory_layout.addStretch(1)
        accessory_box = QGroupBox("云台 / 车灯（云台未接时仅写 I2C/GPIO）")
        accessory_box.setLayout(accessory_layout)

        # --- 视频（独立线程，失败不影响遥控） ---
        self.video_port_spin = QSpinBox()
        self.video_port_spin.setRange(1, 65535)
        self.video_port_spin.setValue(8080)
        self.video_port_spin.setMaximumWidth(90)
        self.video_path_input = QLineEdit(DEFAULT_PATH)
        self.video_path_input.setMaximumWidth(170)
        self.video_checkbox = QCheckBox("启用视频")
        self.video_status_label = QLabel("未启用")
        self.video_status_label.setStyleSheet("color:#757575;")

        video_header = QHBoxLayout()
        video_header.addWidget(QLabel("端口"))
        video_header.addWidget(self.video_port_spin)
        video_header.addWidget(self.video_path_input)
        video_header.addWidget(self.video_checkbox)
        video_header.addStretch(1)
        video_header.addWidget(self.video_status_label)

        self.video_label = QLabel("视频未启用")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setMinimumHeight(240)
        self.video_label.setStyleSheet("background-color:#212121; color:#bdbdbd; border-radius:8px;")

        video_layout = QVBoxLayout()
        video_layout.addLayout(video_header)
        video_layout.addWidget(self.video_label, 1)
        video_box = QGroupBox("视频 MJPEG（Phase 4，摄像头未接时显示不可用）")
        video_box.setLayout(video_layout)

        # --- 日志 ---
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        log_font = self.log_view.font()
        log_font.setFamily("Consolas")
        log_font.setPointSize(9)
        self.log_view.setFont(log_font)

        self.clear_log_button = QPushButton("清空日志")
        self.clear_log_button.setFocusPolicy(Qt.NoFocus)
        log_header = QHBoxLayout()
        log_header.addStretch(1)
        log_header.addWidget(self.clear_log_button)
        log_layout = QVBoxLayout()
        log_layout.addWidget(self.log_view)
        log_layout.addLayout(log_header)
        log_box = QGroupBox("调试日志（时间 / 方向 / packet hex / 连接状态）")
        log_box.setLayout(log_layout)

        hint = QLabel(
            "操作：W 前进 · S 后退 · A 左转 · D 右转 · Space 急停 · Esc 停止并断开。"
            " 松开方向键立即发送 STOP；连接/重连后默认停止。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#616161;")

        root = QVBoxLayout()
        root.addWidget(conn_box)
        middle = QHBoxLayout()
        middle.addWidget(drive_box, 1)
        middle.addWidget(video_box, 1)
        root.addLayout(middle, 2)
        root.addWidget(speed_box)
        root.addWidget(accessory_box)
        root.addWidget(log_box, 3)
        root.addWidget(hint)
        self.setLayout(root)

    def _connect_signals(self) -> None:
        self.connect_button.clicked.connect(self._toggle_connection)
        self.estop_button.clicked.connect(lambda: self.emergency_stop("急停按钮"))
        self.apply_speed_button.clicked.connect(self._apply_speed)
        self.swap_checkbox.toggled.connect(self._on_swap_toggled)
        self.focus_stop_checkbox.toggled.connect(self._on_focus_stop_toggled)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.bridge.status_received.connect(self._on_status)
        self.bridge.log_received.connect(self._append_log)
        self.servo_slider.valueChanged.connect(self._on_servo_slider)
        self.servo_apply_button.clicked.connect(lambda: self._apply_servo())
        self.servo_center_button.clicked.connect(lambda: self._apply_servo(90))
        self.light_on_button.clicked.connect(lambda: self._send_light(True))
        self.light_off_button.clicked.connect(lambda: self._send_light(False))
        self.video_checkbox.toggled.connect(self._toggle_video)
        self.bridge.video_frame.connect(self._on_video_frame)
        self.bridge.video_status.connect(self._on_video_status)

    def _apply_config_to_ui(self) -> None:
        self.host_input.setText(str(self.config["host"]))
        self.port_spin.setValue(int(self.config["port"]))
        self.left_speed_spin.setValue(int(self.config["left_speed"]))
        self.right_speed_spin.setValue(int(self.config["right_speed"]))
        self.swap_checkbox.setChecked(bool(self.config["swap_left_right"]))
        self.focus_stop_checkbox.setChecked(bool(self.config["stop_on_focus_loss"]))

    # ------------------------------------------------------------------
    # 连接流程
    # ------------------------------------------------------------------

    def _toggle_connection(self) -> None:
        if self.client.state is ConnectionState.CONNECTING:
            return
        if self.client.is_connected:
            self.client.disconnect("用户断开")
            return
        host = self.host_input.text().strip()
        if not host:
            self._append_log("请输入小车 IP 地址")
            return
        self.config["host"] = host
        self.config["port"] = self.port_spin.value()
        save_config(self.config)
        self.client.connect_async(host, self.port_spin.value())

    def _status_callback(self, state: ConnectionState, detail: str) -> None:
        # 可能来自连接线程，经 Signal 切回 UI 线程
        self.bridge.status_received.emit(state.value, detail)

    def _on_status(self, state_value: str, detail: str) -> None:
        state = ConnectionState(state_value)
        color, text = _STATE_STYLE[state]
        self.status_dot.setStyleSheet(f"background-color:{color}; border-radius:7px;")
        self.status_label.setText(f"{text} · {detail}" if detail else text)

        connected = state is ConnectionState.CONNECTED
        self.connect_button.setText("断开" if connected else "连接")
        self.connect_button.setEnabled(state is not ConnectionState.CONNECTING)

        if state is ConnectionState.CONNECTED:
            # 重连后默认 STOP、禁止恢复旧运动
            self.machine.clear()
            self.motion_watchdog.disarm()
            self._update_direction_display(None)
            self._warned_disconnected = False
            if self.video_checkbox.isChecked():
                self._start_video()
        elif state in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            self.machine.clear()
            self.motion_watchdog.disarm()
            self._update_direction_display(None)
            if self._video is not None:
                self._stop_video("视频已停止（连接断开）")

        controllable = (
            self.left_speed_spin,
            self.right_speed_spin,
            self.apply_speed_button,
            self.servo_combo,
            self.servo_slider,
            self.servo_apply_button,
            self.servo_center_button,
            self.light_on_button,
            self.light_off_button,
        )
        for widget in controllable:
            widget.setEnabled(connected)

    # ------------------------------------------------------------------
    # 按键处理（应用级事件过滤器，保证按钮/日志聚焦时依然可遥控）
    # ------------------------------------------------------------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            if self._handle_key_event(event):
                return True
        return super().eventFilter(obj, event)

    def _handle_key_event(self, event: QEvent) -> bool:
        is_press = event.type() == QEvent.KeyPress
        key = event.key()

        if key == Qt.Key_Space:
            if is_press and not event.isAutoRepeat():
                self.emergency_stop("Space")
            return True
        if key == Qt.Key_Escape:
            if is_press and not event.isAutoRepeat():
                self.emergency_stop("Esc")
                self.client.disconnect("Esc 断开")
            return True

        text = (event.text() or "").lower()
        if text in DRIVING_KEYS:
            if self._is_text_input_focus():
                return False  # 让 IP/端口输入框正常打字
            if event.isAutoRepeat():
                # 按键自动重复 = "键还按着"的活信号，喂运动看门狗
                if is_press and text == self.machine.active_key:
                    self.motion_watchdog.arm()
                return True
            if is_press:
                self._on_drive_press(text)
            else:
                self._on_drive_release(text)
            return True
        return False

    @staticmethod
    def _is_text_input_focus() -> bool:
        widget = QApplication.focusWidget()
        return isinstance(widget, (QLineEdit, QAbstractSpinBox))

    def _on_drive_press(self, key: str) -> None:
        self.machine.press(key)
        self.motion_watchdog.arm()
        self._dispatch_active_key()

    def _on_drive_release(self, key: str) -> None:
        self.machine.release(key)
        if self.machine.active_key is None:
            self.motion_watchdog.disarm()
        else:
            self.motion_watchdog.arm()
        self._dispatch_active_key()

    def _check_motion_watchdog(self) -> None:
        """keyup 被吞（拖动窗口/系统弹窗等）时，按键状态会失真地一直"按着"。
        超时没有自动重复刷新就急停，杜绝 GUI 侧跑飞。"""
        if not self.machine.pressed or not self.motion_watchdog.should_stop():
            return
        if self.client.is_connected:
            logger.warning(
                "运动刷新超时（%.1fs 未收到按键重复，keyup 可能被吞），自动急停",
                self.motion_watchdog.timeout,
            )
        self.motion_watchdog.disarm()
        self.emergency_stop("运动刷新超时")

    def _dispatch_active_key(self) -> None:
        active = self.machine.active_key
        if self.client.is_connected:
            self.client.send_direction(active)
        elif not self._warned_disconnected:
            self._append_log("未连接：方向指令被忽略（请先连接小车）")
            self._warned_disconnected = True
        self._update_direction_display(active)

    # ------------------------------------------------------------------
    # 安全动作
    # ------------------------------------------------------------------

    def emergency_stop(self, source: str = "") -> None:
        self.machine.clear()
        self.motion_watchdog.disarm()
        if self.client.is_connected:
            self.client.stop(note=f"急停（{source}）")
        self._update_direction_display(None)
        logger.info("急停: %s", source or "未指定")

    def _on_swap_toggled(self, checked: bool) -> None:
        self.client.swap_left_right = checked
        self.config["swap_left_right"] = checked
        save_config(self.config)
        logger.info("左右互换校准: %s", "开" if checked else "关")

    def _on_focus_stop_toggled(self, checked: bool) -> None:
        self.config["stop_on_focus_loss"] = checked
        save_config(self.config)

    def _apply_speed(self) -> None:
        if not self.client.is_connected:
            return
        left = self.left_speed_spin.value()
        right = self.right_speed_spin.value()
        self.client.send_speed(protocol.SPEED_LEFT, left)
        self.client.send_speed(protocol.SPEED_RIGHT, right)
        self.config["left_speed"] = left
        self.config["right_speed"] = right
        save_config(self.config)

    # ------------------------------------------------------------------
    # 云台 / 车灯
    # ------------------------------------------------------------------

    def _on_servo_slider(self, value: int) -> None:
        self.servo_value_label.setText(f"{value}°")

    def _apply_servo(self, angle: Optional[int] = None) -> None:
        if not self.client.is_connected:
            return
        number = self.servo_combo.currentIndex() + 1
        value = self.servo_slider.value() if angle is None else int(angle)
        self.servo_slider.setValue(value)
        self.client.send_servo(number, value)

    def _send_light(self, on: bool) -> None:
        if not self.client.is_connected:
            return
        self.client.send_raw(protocol.light_command(on), note="开灯" if on else "关灯")

    # ------------------------------------------------------------------
    # 视频（独立线程，任何失败都不影响运动控制）
    # ------------------------------------------------------------------

    def _video_url(self) -> str:
        if self._video_url_override:
            return self._video_url_override
        host = self.host_input.text().strip() or "127.0.0.1"
        port = self.video_port_spin.value()
        path = self.video_path_input.text().strip() or DEFAULT_PATH
        if not path.startswith("/"):
            path = "/" + path
        return f"http://{host}:{port}{path}"

    def _toggle_video(self, checked: bool) -> None:
        if checked:
            if self.client.is_connected:
                self._start_video()
            else:
                self.video_status_label.setText("连接成功后将自动启动")
        else:
            self._stop_video("视频已停止")

    def _start_video(self) -> None:
        if self._video is not None and self._video.running:
            return
        self._first_video_frame = True
        self._video = MJPEGReader(
            self._video_url(),
            on_frame=self._on_video_frame_thread,
            on_status=self._on_video_status_thread,
        )
        self.video_label.setText("连接视频中…")
        self._video.start()

    def _stop_video(self, note: str = "") -> None:
        if self._video is not None:
            self._video.stop()
            self._video = None
        if note:
            self.video_status_label.setText(note)
        self.video_label.setPixmap(QPixmap())
        self.video_label.setText("视频未启用")

    def _on_video_frame_thread(self, frame: bytes) -> None:
        self.bridge.video_frame.emit(frame)

    def _on_video_status_thread(self, message: str) -> None:
        self.bridge.video_status.emit(message)

    def _on_video_frame(self, frame: bytes) -> None:
        now = time.monotonic()
        if now - self._last_video_render < 1.0 / 15.0:
            return
        self._last_video_render = now
        image = QImage.fromData(frame)
        if image.isNull():
            return
        pixmap = QPixmap.fromImage(image)
        self.video_label.setPixmap(
            pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        if self._first_video_frame:
            self._first_video_frame = False
            logger.info("视频首帧已接收（%d 字节，%dx%d）", len(frame), image.width(), image.height())

    def _on_video_status(self, message: str) -> None:
        self.video_status_label.setText(message)

    # ------------------------------------------------------------------
    # 界面辅助
    # ------------------------------------------------------------------

    def _update_direction_display(self, key: Optional[str]) -> None:
        self.direction_label.setText(_DIRECTION_TEXT.get(key, "STOP"))
        self.direction_label.setStyleSheet(
            _ACTIVE_DIRECTION_STYLE if key else _INACTIVE_DIRECTION_STYLE
        )

    def _append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def _refresh_stats(self) -> None:
        self.stats_label.setText(f"已发送 {self.client.packets_sent} 帧")

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        if (
            event.type() == QEvent.ActivationChange
            and self.isVisible()
            and not self.isActiveWindow()
            and self.focus_stop_checkbox.isChecked()
            and self.client.is_connected
        ):
            self.emergency_stop("窗口失焦")
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.emergency_stop("程序退出")
        self._stop_video()
        self.client.disconnect("窗口关闭")
        self.config["host"] = self.host_input.text().strip() or self.config["host"]
        self.config["port"] = self.port_spin.value()
        save_config(self.config)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        event.accept()
