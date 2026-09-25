"""持久 TCP 客户端：连接原厂 ``wifirobots.py`` 的 2001 服务并发送控制帧。

设计要点（对应交接文档第 4/6/8 节）：

- 一个持久 socket，绝不每次按键重连；
- 所有写操作加锁（``threading.RLock``）；
- 连接成功后立即发 STOP；断开前 best-effort STOP；
- 每约 10 秒发送心跳 ``FF EF EF EE FF``；
- 网络异常进入 ERROR 状态并拒绝继续发送运动指令（须重连，重连后默认 STOP）；
- 纯 Python 实现，不依赖 Qt，便于单元测试与命令行工具复用。
"""

import logging
import socket
import threading
from enum import Enum
from typing import Callable, Optional

from . import protocol

logger = logging.getLogger(__name__)


class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


StatusCallback = Callable[[ConnectionState, str], None]
LogCallback = Callable[[str], None]


class RobotClient:
    """线程安全的 TCP 遥控客户端。"""

    DEFAULT_PORT = 2001
    DEFAULT_TIMEOUT = 1.5
    SEND_TIMEOUT = 2.0
    HEARTBEAT_INTERVAL = 10.0

    def __init__(
        self,
        status_callback: Optional[StatusCallback] = None,
        log_callback: Optional[LogCallback] = None,
    ) -> None:
        self._sock = None
        self._send_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._state = ConnectionState.DISCONNECTED
        self._detail = ""
        self._host = None
        self._port = self.DEFAULT_PORT
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = None
        self._connect_thread = None

        self.status_callback = status_callback
        self.log_callback = log_callback
        self.packets_sent = 0

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._state_lock:
            return self._detail

    @property
    def host(self) -> Optional[str]:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def is_connected(self) -> bool:
        return self.state is ConnectionState.CONNECTED

    @property
    def peer_ip(self):
        """当前 TCP 连接的对端 IP；未连接返回 None。

        浏览器加载 MJPEG 视频是直连小车 8080，如果地址写 ``car.local``，
        系统代理（Clash 等）解析不了 .local 会 502。用已连上的真实 IP 最稳。
        """
        with self._send_lock:
            sock = self._sock
            if sock is None:
                return None
            try:
                return sock.getpeername()[0]
            except OSError:
                return None

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        with self._state_lock:
            self._state = state
            self._detail = detail
        logger.info("连接状态: %s%s", state.value, (" (%s)" % detail) if detail else "")
        if self.status_callback is not None:
            try:
                self.status_callback(state, detail)
            except Exception:  # pragma: no cover - 防御回调异常
                logger.exception("status_callback 执行异常")

    def _emit(self, message: str) -> None:
        logger.debug(message)
        if self.log_callback is not None:
            try:
                self.log_callback(message)
            except Exception:  # pragma: no cover
                logger.exception("log_callback 执行异常")

    # ------------------------------------------------------------------
    # 连接 / 断开
    # ------------------------------------------------------------------

    def connect(self, host: str, port: Optional[int] = None, timeout: Optional[float] = None) -> bool:
        """同步连接；成功后立即发送 STOP。返回是否成功。"""
        port = int(port or self.DEFAULT_PORT)
        timeout = float(timeout if timeout is not None else self.DEFAULT_TIMEOUT)
        self._teardown(send_stop=False)

        self._set_state(ConnectionState.CONNECTING, "%s:%s" % (host, port))
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(self.SEND_TIMEOUT)
        except OSError as exc:
            self._set_state(ConnectionState.ERROR, "连接失败: %s" % exc)
            self._emit("连接失败 %s:%s -> %s" % (host, port, exc))
            return False

        with self._send_lock:
            self._sock = sock
        self._host, self._port = host, port
        self._set_state(ConnectionState.CONNECTED, "%s:%s" % (host, port))
        self._emit("已连接 %s:%s，先发送 STOP" % (host, port))
        self.send_raw(protocol.STOP, note="连接后默认 STOP")
        self._start_heartbeat()
        return True

    def connect_async(self, host: str, port: Optional[int] = None, timeout: Optional[float] = None) -> None:
        """异步连接，避免阻塞 UI 线程。"""
        if self._connect_thread is not None and self._connect_thread.is_alive():
            return
        self._connect_thread = threading.Thread(
            target=self.connect,
            args=(host, port, timeout),
            name="robot-connect",
            daemon=True,
        )
        self._connect_thread.start()

    def disconnect(self, reason: str = "") -> None:
        """断开：先 best-effort 发 STOP，再关闭 socket、停止心跳。"""
        self._teardown(send_stop=True)
        self._set_state(ConnectionState.DISCONNECTED, reason)

    def _teardown(self, send_stop: bool) -> None:
        self._stop_heartbeat()
        with self._send_lock:
            sock = self._sock
            self._sock = None
            if sock is None:
                return
            if send_stop:
                try:
                    sock.sendall(protocol.STOP)
                    self.packets_sent += 1
                    logger.info("断开前发送 STOP: %s", protocol.packet_hex(protocol.STOP))
                except OSError:
                    pass
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    def send_raw(self, packet: bytes, note: str = "") -> bool:
        """线程安全地发送一帧；失败会把连接置为 ERROR 并返回 False。"""
        if not protocol.is_valid_frame(packet):
            raise ValueError("非法帧: %r" % (packet,))
        with self._send_lock:
            sock = self._sock
            if sock is None:
                self._emit("未连接，丢弃 %s" % protocol.packet_hex(packet))
                return False
            try:
                sock.sendall(packet)
            except OSError as exc:
                self._handle_socket_error("发送失败: %s" % exc)
                return False
            self.packets_sent += 1
        suffix = (" (%s)" % note) if note else ""
        logger.info("发送 %s%s", protocol.packet_hex(packet), suffix)
        return True

    def _handle_socket_error(self, detail: str) -> None:
        self._stop_heartbeat()
        with self._send_lock:
            sock = self._sock
            self._sock = None
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self._emit(detail)
        self._set_state(ConnectionState.ERROR, detail)

    # ------------------------------------------------------------------
    # 语义化命令
    # ------------------------------------------------------------------

    def command_for_key(self, key: Optional[str]) -> bytes:
        """方向键 -> 帧。非法/空键返回 STOP。

        方向帧不做左右互换：实车接线 A=物理右轮、B=物理左轮，固件
        TurnLeft(A 前/B 后)/TurnRight 本身就是正确的物理转向（2026-09-19 回退）。
        """
        key = (key or "").lower()
        if key not in protocol.MOTION_BY_KEY:
            return protocol.STOP
        return protocol.MOTION_BY_KEY[key]

    def send_direction(self, key, note=""):
        packet = self.command_for_key(key)
        label = ("方向 %s" % key.upper()) if key else "STOP"
        return self.send_raw(packet, note=note or label)

    def stop(self, note: str = "STOP") -> bool:
        return self.send_raw(protocol.STOP, note=note)

    def send_speed(self, side: int, percent: int) -> bool:
        side_name = "左" if side == protocol.SPEED_LEFT else "右"
        return self.send_raw(
            protocol.speed_command(side, percent),
            note="%s侧速度 %s%%" % (side_name, protocol.clamp_speed(percent)),
        )

    def send_servo(self, servo_num: int, angle: int) -> bool:
        return self.send_raw(
            protocol.servo_command(servo_num, angle),
            note="舵机 %s -> %s°" % (servo_num, protocol.clamp_angle(angle)),
        )

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        self._heartbeat_stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="robot-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._heartbeat_thread = None

    def _heartbeat_loop(self) -> None:
        logger.info("心跳线程启动，间隔 %.1fs", self.HEARTBEAT_INTERVAL)
        while not self._heartbeat_stop.wait(self.HEARTBEAT_INTERVAL):
            if not self.is_connected:
                break
            self.send_raw(protocol.HEARTBEAT, note="心跳")
        logger.info("心跳线程退出")

    # ------------------------------------------------------------------
    # 上下文管理（测试/工具方便）
    # ------------------------------------------------------------------

    def __enter__(self) -> "RobotClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect("上下文退出")
