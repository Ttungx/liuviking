# -*- coding: utf-8 -*-
"""小R小车网页控制台后端（仅标准库，无第三方依赖）。

同一份代码可以跑在两个位置，只由命令行参数区分::

    车上（树莓派）：python3 web_console.py --bind 0.0.0.0 --port 8082 \
                                       --robot-host 127.0.0.1
        手机/笔记本 --HTTP--> 本服务 --回环 TCP :2001--> 原厂 wifirobots.py

    笔记本：      python src/main.py
        浏览器 --HTTP :8083--> 本服务 --跨网 TCP--> car.local:2001

目标协议端口都是原厂 `wifirobots.py` 的 2001，不改 GPIO、不覆盖原厂固件。

兼容 Python 3.5（树莓派上是 3.5.1）：不用 f-string、不用变量注解、
不用 ``http.server.ThreadingHTTPServer``（3.7+），自己拼 ThreadingMixIn。

安全设计：

- 连接成功即发 STOP、断开前 best-effort STOP（由 :class:`RobotClient` 保证）；
- 运动看门狗：按住方向键期间页面每 0.4s 重发 /api/cmd，服务端超过 1.2s 没刷新
  就自动 STOP；
- /api/stop 无条件发 STOP，任何设备都能急停，不受所有权门控；
- 单控制端：第一个活动的浏览器会话（sid）持有控制权，其它会话收到 busy，
  可以用 /api/claim 抢占，但别人正按住方向键运动时抢不走；
- 会话看门狗：owner 超过 --session-timeout 没有任何请求（关页面/切后台/断网）
  就 STOP 并断开 2001，把控制权让给官方 APP 或另一台设备。
"""

import argparse
import json
import logging
import math
import re
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

if __package__ in (None, ""):  # 允许 python src/web_console.py 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import protocol
from src.config import load_config, save_config
from src.keyboard_controller import MotionRefreshWatchdog
from src.odometry import (
    Odometry,
    polyline_command,
    route_has_sharp_corners,
    smooth_route,
)
from src.robot_client import ConnectionState, RobotClient
from src.video_client import DEFAULT_SNAPSHOT_PATH, fetch_snapshot

logger = logging.getLogger("web_console")


def _web_dir():
    """静态文件目录；PyInstaller 打包后数据文件在 ``sys._MEIPASS`` 下。"""
    bundle = getattr(sys, "_MEIPASS", None)
    base = Path(bundle) if bundle else Path(__file__).resolve().parent
    return base / "web"


WEB_DIR = _web_dir()

# 白名单：只服务这三个文件，路径不来自请求，杜绝目录穿越
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

# ---------------------------------------------------------------------------
# 云台：通道、行程与默认姿态
#
# 2026-09-18 相机位移实测修正：水平轴在协议 7 号、垂直轴在 8 号；
# 水平 + 为左转、垂直 - 为上仰。实车可用行程比固件 Angle_cal 的 15-160 更宽，
# 所以云台走 build_frame 直接下发、按下面的行程钳制，不复用 clamp_angle。
#
# 警告（2026-09-19 实测确认）：B0=0x01 经 2001 必过固件 Angle_cal（15-160），
# 超出部分在真车上会被钳掉（服务端记 185、舵机只到 160，姿态显示撒谎）。
# 全行程必须走 I2C 直控（XR servo 库只有 py2.7 版，py3.5 用不了）或改固件——
# 部署上车前必须先解决此问题，否则把下面的行程改回 15-160 再部署。
# ---------------------------------------------------------------------------

GIMBAL_CHANNELS = {"pan": 7, "tilt": 8}
GIMBAL_LIMITS = {"pan": (0, 185), "tilt": (78, 170)}
GIMBAL_HOME = {"pan": 90, "tilt": 90}

PHOTO_SPACING_MM = 300.0
PHOTO_MIN_SEGMENT_MM = 1500.0
PHOTO_SETTLE_SECONDS = 0.35
PHOTO_PORT = 8080
PHOTO_PATH = DEFAULT_SNAPSHOT_PATH
PHOTO_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
MOTION_FEEDBACK_PORT = 8082
MOTION_FEEDBACK_PATH = "/api/motion_feedback"
MOTION_FEEDBACK_POLL_SECONDS = 0.2
ROUTE_FEEDBACK_TIMEOUT_SECONDS = 1.0
#: 里程计单步积分上限。路线线程拍一组照会握着锁 ~2s，里程计线程憋住后
#: 放开时 dt 陈旧；不封顶就会拿"停车时间 × 恢复指令"补积一大步假位移。
ODOM_MAX_STEP_SECONDS = 0.25


class BusyError(Exception):
    """另一个浏览器会话正持有控制权。"""


def _fetch_motion_feedback(host):
    url = "http://%s:%d%s" % (host, MOTION_FEEDBACK_PORT, MOTION_FEEDBACK_PATH)
    with urlopen(url, timeout=0.4) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not data.get("available"):
        raise RuntimeError(data.get("reason") or "车端反馈不可用")
    return data


# ---------------------------------------------------------------------------
# 日志环形缓冲：把 logging 记录交给浏览器轮询
# ---------------------------------------------------------------------------


class LogBuffer(logging.Handler):
    """保留最近若干条日志，浏览器按序号增量拉取。"""

    def __init__(self, capacity=400):
        logging.Handler.__init__(self)
        self._lock = threading.Lock()
        self._records = []
        self._seq = 0
        self._capacity = capacity
        self.setFormatter(
            logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:  # pragma: no cover - 日志不能拖垮程序
            return
        with self._lock:
            self._seq += 1
            self._records.append({"seq": self._seq, "level": record.levelname, "text": line})
            if len(self._records) > self._capacity:
                del self._records[: len(self._records) - self._capacity]

    def take_since(self, since):
        with self._lock:
            fresh = [r for r in self._records if r["seq"] > since]
            return fresh, self._seq


# ---------------------------------------------------------------------------
# 控制台状态：RobotClient + 运动看门狗 + 会话所有权
# ---------------------------------------------------------------------------


def _resolve_ipv4(host, port):
    """把主机名解析成 IPv4 地址；解析不到返回 None。

    Windows 对 ``.local`` 的 mDNS 解析时灵时不灵（实测有时只回 IPv6、
    有时直接 getaddrinfo failed），所以只取 A 记录，失败由调用方回退缓存 IP。
    """
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    return infos[0][4][0] if infos else None


class Console:
    def __init__(
        self,
        motion_timeout=1.2,
        session_timeout=600.0,
        channels=None,
        require_motion_feedback=True,
    ):
        self.client = RobotClient()
        self.watchdog = MotionRefreshWatchdog(timeout=motion_timeout)
        self.session_timeout = session_timeout
        self.logs = LogBuffer()
        self.channels = dict(GIMBAL_CHANNELS)
        if channels:
            self.channels.update(channels)

        self._host_cache = {}          # host 名 -> 最近一次解析成功的 IP
        self._save_host_cache = None
        self._save_home = None
        self._require_motion_feedback = bool(require_motion_feedback)
        #: 本次路线是否要求车端脉冲反馈；/api/route?fb=0 单次放开为开放环执行
        self._route_require_feedback = self._require_motion_feedback

        self._lock = threading.RLock()
        self._owner = None            # 持有控制权的浏览器会话 id
        self._last_activity = 0.0
        self._last_cmd = None
        self._last_frame = ""
        self._last_frame_at = None
        self.gimbal = dict(GIMBAL_HOME)
        self.home = dict(GIMBAL_HOME)

        # 里程计与路线跟随：按"指令占空比 × 标定满速"开环推算（无编码器）
        self.odom = Odometry()
        self._duty = {"left": 100, "right": 100}   # 最近一次下发的左右占空比
        self._last_odom_t = None
        self._route = []                           # [(x_mm, y_mm), ...]
        self._route_index = 0
        self._route_active = False
        self._route_note = ""
        self._route_stop = threading.Event()
        self._route_tolerance_mm = 12.0
        self._route_last_drive = None
        self._route_allow_arc = False
        self._photo_schedule = {}
        self._photo_cursor = {}
        self._photo_count = 0
        self._photo_disabled = False
        self._photo_route_dir = None
        self._photo_manifest = []
        self._motion_feedback = {
            "available": False,
            "reason": "尚未读取车端反馈",
            "pulse1": None,
            "pulse2": None,
            "voltage": None,
        }
        self._motion_feedback_at = 0.0
        self._route_motion_started_at = None
        self._route_motion_baseline = None
        self._route_motion_last_sample = None
        self._route_motion_seen = False

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._supervisor, name="console-watchdog")
        self._thread.daemon = True
        self._thread.start()
        self._odom_thread = threading.Thread(target=self._odom_loop, name="console-odom")
        self._odom_thread.daemon = True
        self._odom_thread.start()
        self._feedback_thread = threading.Thread(target=self._feedback_loop, name="console-feedback")
        self._feedback_thread.daemon = True
        self._feedback_thread.start()

    # -- 会话所有权（调用方需持有锁） ------------------------------------

    def _take(self, sid):
        """请求控制权：别的会话正在活跃持有时抛 BusyError。

        无 sid 的单用户请求直接放行；已有活跃 owner 时不续期（防旁观者续命）。
        """
        now = time.monotonic()
        if (
            sid
            and self._owner is not None
            and self._owner != sid
            and now - self._last_activity <= self.session_timeout
        ):
            raise BusyError()
        if sid:
            self._owner = sid
            self._last_activity = now
        elif self._owner is None:
            self._last_activity = now

    def _touch(self, sid=None):
        """刷新活动时间；旁观者（异 sid，或无 sid 但已有活跃 owner）既不续期也不抢权。"""
        now = time.monotonic()
        if sid and (self._owner is None or now - self._last_activity > self.session_timeout):
            self._owner = sid
        if self._owner is None or (sid and self._owner == sid):
            self._last_activity = now

    # -- 连接 ------------------------------------------------------------

    def set_host_cache(self, mapping, save=None):
        """主机名 -> 可用 IPv4 缓存（.local 解析失败时兜底）；save 回调用于持久化。"""
        self._host_cache = dict(mapping or {})
        self._save_host_cache = save

    def set_home_persistence(self, pose, save=None):
        """加载并持久化云台归位点；保存失败不影响当前控制。"""
        if isinstance(pose, dict):
            try:
                self.home = {
                    "pan": max(GIMBAL_LIMITS["pan"][0], min(GIMBAL_LIMITS["pan"][1], int(pose.get("pan", self.home["pan"])))),
                    "tilt": max(GIMBAL_LIMITS["tilt"][0], min(GIMBAL_LIMITS["tilt"][1], int(pose.get("tilt", self.home["tilt"])))),
                }
            except (TypeError, ValueError):
                logger.warning("归位点配置无效，继续使用 %s", self.home)
        self._save_home = save

    def _prefer_ipv4(self, host, port):
        """``.local`` 先解析成 IPv4（mDNS 在 Windows 上不稳）；解析不动就用缓存 IP。"""
        if not host.endswith(".local"):
            return host
        ip = _resolve_ipv4(host, port)
        if ip is None:
            cached = self._host_cache.get(host)
            if cached:
                logger.warning("%s 解析失败，回退到最近可用 IP %s", host, cached)
            return cached or host
        if self._host_cache.get(host) != ip:
            self._host_cache[host] = ip
            if self._save_host_cache is not None:
                try:
                    self._save_host_cache(self._host_cache)
                except Exception:  # pragma: no cover - 配置落盘失败不挡连接
                    logger.exception("保存主机 IP 缓存失败")
        return ip

    def connect(self, sid, host, port):
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if not host:
                return self._error("请输入小车地址")
            if self.client.state is ConnectionState.CONNECTING:
                return self.status(sid)
            host = self._prefer_ipv4(host, port)
            self.client.connect_async(host, port)
            self.watchdog.disarm()
            self._last_cmd = None
            return self.status(sid)

    def disconnect(self, sid=None, reason="用户断开"):
        with self._lock:
            self._touch(sid)
            self._abort_route_locked("连接断开")
            self.watchdog.disarm()
            self._last_cmd = None
            self._last_frame = ""
            self._last_frame_at = None
            self.client.disconnect(reason)
            self._owner = None
            return self.status()

    def claim(self, sid):
        """显式抢占：当前没有未完成的运动指令时才允许。"""
        with self._lock:
            if (
                self._owner is not None
                and self._owner != sid
                and self._last_cmd is not None
            ):
                status = self._busy_status()
                status["reason"] = "对方正按住方向键，抢不过来"
                return status
            if self.client.is_connected:
                self.client.stop(note="抢占前停车")
            self._owner = sid
            self._last_cmd = None
            self._last_activity = time.monotonic()
            logger.info("控制权交给会话 %.6s", sid or "-")
            return self.status(sid)

    # -- 指令 ------------------------------------------------------------

    def command(self, sid, key):
        if key not in protocol.MOTION_BY_KEY:
            raise ValueError("invalid key: %r" % (key,))
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if self._route_active:
                return self._error("路线行驶中：先停止路线")
            if not self.client.is_connected:
                return self._error("未连接：方向指令被忽略")
            self.client.send_direction(key)
            self.watchdog.arm()
            self._last_cmd = key
            self._note_frame(self.client.command_for_key(key))
            return self.status(sid)

    def stop(self, sid=None, reason="浏览器松开"):
        """无条件急停：任何设备都能停，也不要求已经连接。"""
        with self._lock:
            self._touch(sid)
            self._abort_route_locked("急停（%s）" % reason)
            self.watchdog.disarm()
            self._last_cmd = None
            if self.client.is_connected:
                self.client.stop(note="急停（%s）" % reason)
                self._note_frame(protocol.STOP)
            return self.status(sid)

    def speed(self, sid, side, percent):
        side_byte = {"left": protocol.SPEED_LEFT, "right": protocol.SPEED_RIGHT}.get(side)
        if side_byte is None:
            raise ValueError("invalid side: %r" % (side,))
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if self._route_active:
                return self._error("路线行驶中：先停止路线")
            if not self.client.is_connected:
                return self._error("未连接")
            percent = protocol.clamp_speed(percent)
            self.client.send_speed(side_byte, percent)
            self._duty[side] = percent
            return self.status(sid)

    def servo(self, sid, number, angle):
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if not self.client.is_connected:
                return self._error("未连接")
            self.client.send_servo(number, angle)
            return self.status(sid)

    def _set_gimbal_locked(self, pan=None, tilt=None, note_prefix="云台"):
        """已持锁时设置云台，只在帧发送成功后更新服务端姿态。"""
        if not self.client.is_connected:
            return False
        for axis, raw in (("pan", pan), ("tilt", tilt)):
            if raw is None:
                continue
            low, high = GIMBAL_LIMITS[axis]
            target = max(low, min(high, int(raw)))
            if target == self.gimbal[axis]:
                continue
            name = "水平" if axis == "pan" else "垂直"
            frame = protocol.build_frame(0x01, self.channels[axis], target)
            if not self.client.send_raw(frame, note="%s %s -> %d°" % (note_prefix, name, target)):
                return False
            self.gimbal[axis] = target
        return True

    def set_gimbal(self, sid, pan, tilt):
        """无级调节：把两轴设到目标角度，只下发真正变化了的轴。"""
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if not self._set_gimbal_locked(pan, tilt):
                return self._error("未连接或发送失败")
            return self.status(sid)

    def mark_home(self, sid=None):
        """把当前姿态记为归位点（归位是服务端权威值，不是页面估算）。"""
        with self._lock:
            self._touch(sid)
            self.home = dict(self.gimbal)
            if self._save_home is not None:
                try:
                    self._save_home(dict(self.home))
                except Exception:
                    logger.exception("保存云台归位点失败")
            logger.info("云台归位点已设为 水平 %d° / 垂直 %d°", self.home["pan"], self.home["tilt"])
            return self.status(sid)

    def light(self, sid, on):
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if not self.client.is_connected:
                return self._error("未连接")
            self.client.send_raw(protocol.light_command(on), note="开灯" if on else "关灯")
            return self.status(sid)

    # -- 里程计与路线跟随 ------------------------------------------------

    def _odom_loop(self):
        while not self._stop_event.wait(0.05):
            try:
                self._odom_tick()
            except Exception:  # pragma: no cover - 里程计线程不能死
                logger.exception("里程计积分异常")

    def _feedback_loop(self):
        while not self._stop_event.wait(MOTION_FEEDBACK_POLL_SECONDS):
            with self._lock:
                if not self._route_active:
                    continue  # 路线行驶中持续采样，供状态显示与门控共用
                host = self.client.peer_ip if self.client.is_connected else None
            if not host:
                continue
            try:
                sample = _fetch_motion_feedback(host)
            except Exception as exc:
                sample = {
                    "available": False,
                    "reason": str(exc),
                    "pulse1": None,
                    "pulse2": None,
                    "voltage": None,
                }
            with self._lock:
                self._motion_feedback = sample
                self._motion_feedback_at = time.monotonic()

    def _odom_tick(self):
        now = time.monotonic()
        with self._lock:
            last = self._last_odom_t
            self._last_odom_t = now
            if last is None:
                return
            key = self._last_cmd if self.client.is_connected else None
            v_left, v_right = self.odom.wheel_speeds(
                key, self._duty["left"], self._duty["right"]
            )
            step = min(now - last, ODOM_MAX_STEP_SECONDS)
            self.odom.integrate(v_left, v_right, step)

    def odom_snapshot(self, sid=None):
        with self._lock:
            self._touch(sid)
            snap = self.odom.snapshot()
            snap["ok"] = True
            snap["reason"] = ""
            snap["route"] = {
                "active": self._route_active,
                "index": self._route_index,
                "total": len(self._route),
                "tolerance_mm": self._route_tolerance_mm,
                "note": self._route_note,
                "require_feedback": self._route_require_feedback,
                "waypoints": list(self._route),
                "photos": self._photo_status_locked(),
                "motion_feedback": self._motion_feedback_snapshot_locked(),
            }
            return snap

    def _motion_feedback_snapshot_locked(self):
        payload = dict(self._motion_feedback)
        payload["age"] = (
            round(max(0.0, time.monotonic() - self._motion_feedback_at), 1)
            if self._motion_feedback_at else None
        )
        return payload

    def configure_odom(
        self,
        sid,
        track_mm=None,
        max_speed_mm_per_s=None,
        left_scale=None,
        right_scale=None,
        deadzone=None,
    ):
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            self.odom.configure(
                track_mm,
                max_speed_mm_per_s,
                left_scale,
                right_scale,
                deadzone,
                deadzone,
            )
            return self.odom_snapshot(sid)

    def reset_odom(self, sid=None, x=0.0, y=0.0, theta=0.0):
        with self._lock:
            self._touch(sid)
            self.odom.reset(x, y, theta)
            return self.odom_snapshot(sid)

    @staticmethod
    def _build_photo_schedule(points):
        route = [(0.0, 0.0)] + [tuple(point) for point in points]
        schedule = {}
        for index, (start, end) in enumerate(zip(route, route[1:])):
            length = math.hypot(end[0] - start[0], end[1] - start[1])
            if length <= PHOTO_MIN_SEGMENT_MM:
                continue
            distances = [float(distance) for distance in range(0, int(length) + 1, int(PHOTO_SPACING_MM))]
            if not distances or distances[-1] < length:
                distances.append(length)
            schedule[index] = distances
        return schedule

    def _photo_status_locked(self):
        total = sum(len(items) for items in self._photo_schedule.values())
        done = sum(min(self._photo_cursor.get(index, 0), len(items)) for index, items in self._photo_schedule.items())
        return {
            "enabled": bool(self._photo_schedule),
            "done": done,
            "total": total,
            "directory": str(self._photo_route_dir) if self._photo_route_dir else "",
        }

    def _reset_route_motion_feedback_locked(self):
        self._route_motion_started_at = None
        self._route_motion_baseline = None
        self._route_motion_last_sample = None
        self._route_motion_seen = False

    def _clear_motion_feedback_locked(self):
        self._motion_feedback = {
            "available": False,
            "reason": "等待车端反馈",
            "pulse1": None,
            "pulse2": None,
            "voltage": None,
        }
        self._motion_feedback_at = 0.0

    def _route_motion_feedback_reason_locked(self, now):
        if not self._route_require_feedback or self._route_motion_started_at is None:
            return None
        sample = self._motion_feedback
        if not sample.get("available"):
            if now - self._route_motion_started_at >= ROUTE_FEEDBACK_TIMEOUT_SECONDS:
                return "无实际运动反馈（车端脉冲接口不可用）"
            return None
        pulses = (sample.get("pulse1"), sample.get("pulse2"))
        if None in pulses:
            return "无实际运动反馈（车端脉冲数据无效）"
        if self._route_motion_baseline is None:
            self._route_motion_baseline = pulses
            self._route_motion_last_sample = now
            return None
        if pulses != self._route_motion_baseline:
            self._route_motion_baseline = pulses
            self._route_motion_last_sample = now
            self._route_motion_seen = True
            return None
        if now - self._route_motion_last_sample >= ROUTE_FEEDBACK_TIMEOUT_SECONDS:
            return "无实际运动反馈（脉冲计数未变化）"
        return None

    def _photo_due_locked(self, segment_index, start, end):
        if self._photo_disabled or segment_index not in self._photo_schedule:
            return None
        distances = self._photo_schedule[segment_index]
        cursor = self._photo_cursor.get(segment_index, 0)
        if cursor >= len(distances):
            return None
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            return None
        along = ((self.odom.x - start[0]) * dx + (self.odom.y - start[1]) * dy) / length
        along = max(0.0, min(length, along))
        target = distances[cursor]
        # 没观测到实际运动（脉冲无变化）时，后续拍照点不按开环估算推进；起点组
        # （target=0）照拍，之后交给反馈超窗/到达检查收尾（2026-09-25 真机：估算
        # 一到 300mm 就抢在 1s 超窗前触发拍照，路线照样跑完）。
        if target > 0 and self._route_require_feedback and not self._route_motion_seen:
            return None
        if target <= along + max(8.0, self._route_tolerance_mm):
            self._photo_cursor[segment_index] = cursor + 1
            return target
        return None

    def _photo_url(self):
        host = self.client.peer_ip or self.client.host
        if not host:
            return None
        path = PHOTO_PATH if PHOTO_PATH.startswith("/") else "/" + PHOTO_PATH
        return "http://%s:%d%s" % (host, PHOTO_PORT, path)

    def _save_photo_locked(self, data, segment_index, distance_mm, label, tilt):
        if self._photo_route_dir is None:
            self._photo_route_dir = PHOTO_DATA_DIR / ("route_" + time.strftime("%Y%m%d_%H%M%S"))
            self._photo_route_dir.mkdir(parents=True, exist_ok=True)
        name = "seg%02d_%04dmm_%s.jpg" % (segment_index + 1, round(distance_mm), label)
        path = self._photo_route_dir / name
        path.write_bytes(data)
        self._photo_manifest.append({
            "file": name,
            "segment": segment_index + 1,
            "distance_mm": round(distance_mm, 1),
            "pan": self.gimbal["pan"],
            "tilt": tilt,
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        (self._photo_route_dir / "manifest.json").write_text(
            json.dumps(self._photo_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def _capture_photo_sequence_locked(self, segment_index, distance_mm):
        """停车后完成一组三张照片；下一组从上一次最高仰角向下拍。"""
        if self._photo_disabled:
            return
        url = self._photo_url()
        if not url:
            self._photo_disabled = True
            logger.warning("路线拍照跳过：没有可用的摄像头地址")
            return
        self.watchdog.disarm()
        self._last_cmd = None
        # 拍照期间车位不动且本函数握着锁 ~2s：放开锁后让里程计线程重新对时，
        # 否则会拿陈旧 dt × 恢复行驶的指令补积假位移（2026-09-25 真机）。
        self._last_odom_t = None
        if self.client.is_connected:
            self.client.stop(note="路线拍照暂停")
            self._note_frame(protocol.STOP)
        pan_right = GIMBAL_LIMITS["pan"][0]  # 当前实车映射：水平负方向为车体右方
        level = max(GIMBAL_LIMITS["tilt"][0], min(GIMBAL_LIMITS["tilt"][1], int(self.home["tilt"])))
        peak = GIMBAL_LIMITS["tilt"][0]
        middle = int(round((level + peak) / 2.0))
        angles = [("level", level), ("middle", middle), ("peak", peak)]
        if self._photo_count % 2:
            angles.reverse()
        for label, tilt in angles:
            if not self._set_gimbal_locked(pan_right, tilt, "拍照云台"):
                logger.warning("路线拍照云台调整失败：%s", label)
            time.sleep(PHOTO_SETTLE_SECONDS)
            try:
                data = fetch_snapshot(url)
                path = self._save_photo_locked(data, segment_index, distance_mm, label, tilt)
                logger.info("路线照片已保存：%s", path)
            except Exception as exc:
                self._photo_disabled = True
                logger.warning("路线拍照失败，后续拍照停用：%s", exc)
                break
        self._photo_count += 1
        # 拍照期间已发送 STOP，旧的运动签名不能复用；否则路线循环会
        # 误以为指令没变而跳过恢复行驶，随后被“卡住”检测误报。
        self._route_last_drive = None
        self._reset_route_motion_feedback_locked()

    def start_route(
        self,
        sid,
        waypoints,
        track_mm=None,
        max_speed_mm_per_s=None,
        tolerance_mm=None,
        left_scale=None,
        right_scale=None,
        deadzone=None,
        initial_theta_deg=0.0,
        require_feedback=None,
    ):
        """开始按路点行驶：当前位置当原点，初始车头方向由调用方给出。

        ``require_feedback`` 缺省沿用控制台默认（真），传 False 即开放环执行：
        不因脉冲无变化停车、拍照点按开环估算推进（本车无编码器时的可用模式，
        轨迹仍是估算，不代表车体真实位置）。
        """
        with self._lock:
            try:
                self._take(sid)
            except BusyError:
                return self._busy_status()
            if self._route_active:
                return self._error("路线已在行驶中")
            if not self.client.is_connected:
                return self._error("未连接：无法开始路线")
            raw_points = _parse_waypoints(waypoints)
            if not raw_points:
                return self._error("路点为空：每行一个 x,y（mm）")
            points = smooth_route(raw_points)
            self.odom.configure(
                track_mm,
                max_speed_mm_per_s,
                left_scale,
                right_scale,
                deadzone,
                deadzone,
            )
            if tolerance_mm is not None:
                # 大容差会在拐角前提前切弯；停止距离应单独建模，不能混入路点容差。
                self._route_tolerance_mm = max(5.0, min(40.0, float(tolerance_mm)))
            self.odom.reset(theta=math.radians(float(initial_theta_deg or 0.0)))
            self._route = points
            self._route_allow_arc = len(points) > 1 and not route_has_sharp_corners(raw_points)
            self._photo_schedule = self._build_photo_schedule(points)
            self._photo_cursor = {index: 0 for index in self._photo_schedule}
            self._photo_count = 0
            self._photo_disabled = False
            self._photo_route_dir = None
            self._photo_manifest = []
            self._route_index = 0
            self._route_note = "行驶中"
            self._route_active = True
            if require_feedback is None:
                self._route_require_feedback = self._require_motion_feedback
            else:
                self._route_require_feedback = bool(require_feedback)
            self._clear_motion_feedback_locked()
            self._route_last_drive = None
            self._reset_route_motion_feedback_locked()
            self._route_stop.clear()
            self.watchdog.arm()
            self._last_cmd = None
            threading.Thread(target=self._route_loop, name="console-route", daemon=True).start()
            logger.info("开始路线：%d 个路点，容差 %.0fmm", len(points), self._route_tolerance_mm)
            return self.odom_snapshot(sid)

    def stop_route(self, sid=None, reason="用户停止路线"):
        with self._lock:
            self._touch(sid)
            self._abort_route_locked(reason)
            self.watchdog.disarm()
            self._last_cmd = None
            if self.client.is_connected:
                self.client.stop(note="路线停止（%s）" % reason)
                self._note_frame(protocol.STOP)
            return self.odom_snapshot(sid)

    def _abort_route_locked(self, note):
        """已持锁时中止路线（急停/断开复用）。"""
        if self._route_active:
            self._route_active = False
            self._route_note = note
            self._route_stop.set()

    def _send_drive_locked(self, cmd):
        """路线行驶专用（已持锁）：下发轮速与方向帧。"""
        left, right = cmd["left"], cmd["right"]
        self.client.send_speed(protocol.SPEED_LEFT, left)
        self.client.send_speed(protocol.SPEED_RIGHT, right)
        self._duty["left"], self._duty["right"] = left, right
        self.client.send_direction(cmd["key"])
        self._last_cmd = cmd["key"]
        self._note_frame(self.client.command_for_key(cmd["key"]))

    def _route_loop(self):
        note = "完成"
        deadline = time.monotonic() + 600.0
        try:
            while not self._route_stop.is_set():
                with self._lock:
                    if not self.client.is_connected:
                        note = "连接断开"
                        break
                    if self._route_index >= len(self._route):
                        break
                    tx, ty = self._route[self._route_index]
                    start = (0.0, 0.0) if self._route_index == 0 else self._route[self._route_index - 1]
                    if start == (tx, ty):
                        self._route_index += 1
                        self._route_last_drive = None
                        continue
                    photo_distance = self._photo_due_locked(self._route_index, start, (tx, ty))
                    if photo_distance is not None:
                        self._capture_photo_sequence_locked(self._route_index, photo_distance)
                        continue
                    feedback_note = self._route_motion_feedback_reason_locked(time.monotonic())
                    if feedback_note:
                        note = feedback_note
                        break
                    dx, dy = tx - self.odom.x, ty - self.odom.y
                    if math.hypot(dx, dy) <= self._route_tolerance_mm:
                        if (self._route_require_feedback
                                and self._route_motion_started_at is not None
                                and not self._route_motion_seen):
                            note = "无实际运动反馈（未检测到脉冲变化）"
                            break
                        logger.info("到达路点 %d/%d", self._route_index + 1, len(self._route))
                        self._route_index += 1
                        self._route_last_drive = None
                        continue
                    cmd = polyline_command(
                        self.odom.x,
                        self.odom.y,
                        self.odom.theta,
                        start,
                        (tx, ty),
                        allow_arc=self._route_allow_arc,
                        pivot_deg=10.0 if self._route_allow_arc else 4.0,
                        pivot_duty=45 if self._route_allow_arc else 50,
                        min_inner=90,
                        turn_mode="auto",
                    )
                    signature = (cmd["key"], cmd["left"], cmd["right"])
                    if signature != self._route_last_drive:
                        self._send_drive_locked(cmd)
                        self._route_last_drive = signature
                        if self._route_motion_started_at is None:
                            # 只在“从静止恢复行驶”时重置反馈窗口（发车、拍照暂停后恢复）；
                            # 连续行驶中的转向修正重发不重置——实测重发达 10-20Hz，重置会
                            # 让“脉冲 1s 无变化”超窗永远累计不起来，路线照跑（2026-09-25 真机）。
                            self._route_motion_started_at = time.monotonic()
                            self._route_motion_baseline = None
                            self._route_motion_last_sample = None
                            self._route_motion_seen = False
                    self.watchdog.arm()
                now = time.monotonic()
                with self._lock:
                    feedback_note = self._route_motion_feedback_reason_locked(now)
                    if feedback_note:
                        note = feedback_note
                        break
                if now >= deadline:
                    note = "超时（10 分钟）"
                    break
                time.sleep(0.05)
        except Exception as exc:  # pragma: no cover - 路线线程不能死
            logger.exception("路线行驶异常")
            note = "异常：%s" % exc
        finally:
            with self._lock:
                self._route_active = False
                self._route_note = note
                self._route_stop.set()
                self.watchdog.disarm()
                self._last_cmd = None
                if self.client.is_connected:
                    self.client.stop(note="路线结束（%s）" % note)
                    self._note_frame(protocol.STOP)
                if self._photo_count and self.client.is_connected:
                    self._set_gimbal_locked(self.home["pan"], self.home["tilt"], "路线结束归位")
            logger.info("路线结束：%s", note)

    # -- 状态 ------------------------------------------------------------

    def status(self, sid=None):
        """带 sid 时兼任活动心跳：只有 owner 的轮询会续期。"""
        with self._lock:
            self._touch(sid)
            owner = self._owner
            return {
                "ok": True,
                "reason": "",
                "state": self.client.state.value,
                "detail": self.client.detail,
                "packets": self.client.packets_sent,
                "last": self._last_cmd,
                "frame": self._last_frame,
                "frame_age": self._frame_age(),
                "motion_armed": self.watchdog.armed,
                "peer": self.client.peer_ip,
                "autopilot": self._route_active,
                "gimbal": dict(self.gimbal),
                "home": dict(self.home),
                "gimbal_limits": dict((k, list(v)) for k, v in GIMBAL_LIMITS.items()),
                "gimbal_channels": dict(self.channels),
                "busy": bool(owner and (not sid or owner != sid)),
                "mine": bool(sid and owner == sid),
                "session_left": round(
                    max(0.0, self.session_timeout - (time.monotonic() - self._last_activity)), 1
                ),
            }

    def _frame_age(self):
        if self._last_frame_at is None:
            return None
        return round(time.monotonic() - self._last_frame_at, 1)

    def _note_frame(self, packet):
        self._last_frame = protocol.packet_hex(packet)
        self._last_frame_at = time.monotonic()

    def _error(self, reason):
        status = self.status()
        status["ok"] = False
        status["reason"] = reason
        return status

    def _busy_status(self):
        status = self.status()
        status["ok"] = False
        status["busy"] = True
        status["rejected"] = "busy"  # HTTP 层只认这个显式标记映射 423
        status["reason"] = "busy"
        return status

    # -- 看门狗 ----------------------------------------------------------

    def _supervisor(self):
        while not self._stop_event.wait(0.4):
            try:
                self._sweep()
            except Exception:  # pragma: no cover - 监督线程不能死
                logger.exception("看门狗异常")

    def _sweep(self):
        with self._lock:
            now = time.monotonic()
            if self.client.is_connected and self.watchdog.should_stop(now):
                logger.warning("运动刷新超时 %.1fs 无刷新，自动 STOP", self.watchdog.timeout)
                self.watchdog.disarm()
                self._last_cmd = None
                self.client.stop(note="急停（运动刷新超时）")
                # 超时只停车，避免把弧线转向中的内侧轮拽回静摩擦死区；
                # 对称速度由下一条直行命令重新下发。
                self._note_frame(protocol.STOP)
            # 会话看门狗：路线自动执行期间保持长连接；普通页面长时间离开后
            # 才 STOP 并断开 2001，避免后台标签页节流造成误断线。
            # 注意：只有“能续期”的请求才会刷新 _last_activity（见 _take/_touch），
            # 旁观者的轮询续不了命，死会话一定能被回收。
            if (
                self.client.is_connected
                and not self._route_active
                and now - self._last_activity > self.session_timeout
            ):
                logger.info("会话 %.6s 超过 %.0fs 无请求，STOP 并断开 2001 让出控制权",
                            self._owner, self.session_timeout)
                self.disconnect(None, "页面无活动")

    def close(self):
        self._stop_event.set()
        self.client.disconnect("服务退出")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def make_handler(console):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "XiaoRConsole/1.0"

        # -- 响应辅助 ---------------------------------------------------

        def _send(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, payload, status=200):
            # 423 只表示“因被锁定而拒绝”，由 _busy_status 的 rejected 标记显式声明。
            # 不能看 busy 字段：它只是“当前有主”的信息（无 sid 的 _error 响应也会
            # 带着 busy=True，曾把“未连接：无法开始路线”错标成 423 busy）。
            code = 423 if (payload.get("rejected") == "busy" and not payload.get("ok")) else status
            self._send(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                code,
            )

        def _static(self, name, content_type):
            try:
                body = (WEB_DIR / name).read_bytes()
            except OSError:
                self._json({"ok": False, "reason": "缺少前端文件 %s" % name}, 500)
                return
            self._send(body, content_type)

        # -- 路由 -------------------------------------------------------

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                self._route(parsed.path, params)
            except ValueError as exc:
                self._json({"ok": False, "reason": str(exc)}, 400)
            except Exception as exc:  # pragma: no cover - 单请求不拖垮服务
                logger.exception("请求处理失败 %s", parsed.path)
                self._json({"ok": False, "reason": str(exc)}, 500)

        def _route(self, path, params):
            sid = _str(params, "sid")
            if path in STATIC_FILES:
                name, content_type = STATIC_FILES[path]
                self._static(name, content_type)
            elif path == "/api/status":
                self._json(console.status(sid))
            elif path == "/api/logs":
                fresh, seq = console.logs.take_since(_int(params, "since", 0))
                self._json({"ok": True, "seq": seq, "lines": fresh})
            elif path == "/api/connect":
                self._json(console.connect(sid, _str(params, "host"), _int(params, "port", 2001)))
            elif path == "/api/disconnect":
                self._json(console.disconnect(sid))
            elif path == "/api/claim":
                self._json(console.claim(sid))
            elif path == "/api/cmd":
                self._json(console.command(sid, _str(params, "k").lower()))
            elif path == "/api/stop":
                self._json(console.stop(sid, _str(params, "why") or "浏览器"))
            elif path == "/api/speed":
                self._json(console.speed(sid, _str(params, "side"), _int(params, "value", 100)))
            elif path == "/api/servo":
                self._json(console.servo(sid, _int(params, "n", 1), _int(params, "a", 90)))
            elif path == "/api/gimbal":
                self._json(
                    console.set_gimbal(sid, _opt_int(params, "pan"), _opt_int(params, "tilt"))
                )
            elif path == "/api/gimbal_home":
                self._json(console.mark_home(sid))
            elif path == "/api/light":
                self._json(console.light(sid, _str(params, "on") in ("1", "true", "on")))
            elif path == "/api/odom":
                self._json(console.odom_snapshot(sid))
            elif path == "/api/odom_cfg":
                self._json(console.configure_odom(
                    sid,
                    _opt_float(params, "track"),
                    _opt_float(params, "vmax"),
                    _opt_float(params, "left"),
                    _opt_float(params, "right"),
                    _opt_float(params, "dead"),
                ))
            elif path == "/api/odom_reset":
                self._json(console.reset_odom(
                    sid,
                    _opt_float(params, "x") or 0.0,
                    _opt_float(params, "y") or 0.0,
                    math.radians(_opt_float(params, "theta_deg") or 0.0),
                ))
            elif path == "/api/route":
                self._json(console.start_route(
                    sid,
                    _str(params, "wp"),
                    _opt_float(params, "track"),
                    _opt_float(params, "vmax"),
                    _opt_float(params, "tol"),
                    _opt_float(params, "left"),
                    _opt_float(params, "right"),
                    _opt_float(params, "dead"),
                    _opt_float(params, "heading") or 0.0,
                    _opt_int(params, "fb"),
                ))
            elif path == "/api/route_stop":
                self._json(console.stop_route(sid))
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", 204)
            else:
                self._json({"ok": False, "reason": "not found"}, 404)

        def handle(self):
            try:
                BaseHTTPRequestHandler.handle(self)
            except OSError:
                pass  # 浏览器刷新/关闭导致的中断，静默

        def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏盖过控制日志
            return

    return Handler


def _str(params, name, default=""):
    return (params.get(name) or [default])[0].strip()


def _int(params, name, default):
    raw = _str(params, name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _opt_int(params, name):
    """缺省与非法都返回 None：表示这一轴不动。"""
    raw = _str(params, name)
    try:
        return int(float(raw)) if raw else None
    except ValueError:
        return None


def _opt_float(params, name):
    raw = _str(params, name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _parse_waypoints(raw):
    """解析 ``x,y;x,y`` 或换行分隔的路点串；非法抛 ValueError。"""
    points = []
    for chunk in re.split(r"[;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        parts = [p for p in re.split(r"[,\s]+", chunk) if p]
        if len(parts) < 2:
            raise ValueError("路点格式应为 x,y：%s" % chunk)
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            raise ValueError("路点不是数字：%s" % chunk)
        points.append((round(x, 1), round(y, 1)))
        if len(points) > 200:
            raise ValueError("路点过多（最多 200 个）")
    return points


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Python 3.5 没有 http.server.ThreadingHTTPServer（3.7+），自己拼一个。"""

    daemon_threads = True
    allow_reuse_address = True


def build_server(console, bind, port):
    return ThreadingHTTPServer((bind, port), make_handler(console))


def build_arg_parser():
    parser = argparse.ArgumentParser(description="小R科技 WiFi 小车网页控制台")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="监听地址；部署到车上给手机用时填 0.0.0.0")
    parser.add_argument("--port", type=int, default=8083,
                        help="HTTP 端口，本机默认 8083；车上沿用 8082")
    parser.add_argument("--robot-host", default=None,
                        help="原厂 2001 服务地址：跑在车上填 127.0.0.1，"
                             "跑在笔记本填小车地址（car.local），不填则用本地配置")
    parser.add_argument("--robot-port", type=int, default=None, help="小车控制端口，默认 2001")
    parser.add_argument("--connect", action="store_true", help="启动后自动连接")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--motion-timeout", type=float, default=1.2,
                        help="运动命令刷新超时（秒）后自动 STOP")
    parser.add_argument("--session-timeout", type=float, default=600.0,
                        help="普通页面多少秒无请求后才 STOP 并断开 2001；路线执行期间保持长连接")
    parser.add_argument("--pan-servo", type=int, default=GIMBAL_CHANNELS["pan"],
                        help="云台水平轴舵机通道（2026-09-18 实车实测为 7）")
    parser.add_argument("--tilt-servo", type=int, default=GIMBAL_CHANNELS["tilt"],
                        help="云台垂直轴舵机通道（2026-09-18 实车实测为 8）")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def setup_logging(console, level):
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger().addHandler(console.logs)


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    console = Console(
        motion_timeout=args.motion_timeout,
        session_timeout=args.session_timeout,
        channels={"pan": args.pan_servo, "tilt": args.tilt_servo},
    )
    setup_logging(console, args.log_level)

    # 笔记本模式不填 --robot-host 时回落到本地配置；车上模式由启动脚本显式填 127.0.0.1
    config = load_config()

    def save_host_ips(mapping):
        config["host_ips"] = dict(mapping)
        save_config(config)

    def save_home(pose):
        config["gimbal_home"] = dict(pose)
        save_config(config)

    console.set_home_persistence(config.get("gimbal_home"), save_home)
    host = args.robot_host or str(config["host"])
    port = args.robot_port or int(config["port"])
    # .local 解析失败的兜底：记住每个主机名最近一次成功的 IP，落盘跨重启有效
    console.set_host_cache(
        config.get("host_ips"),
        save_host_ips,
    )

    server = build_server(console, args.bind, args.port)
    url = "http://%s:%d/" % (args.bind, args.port)
    print("[console] 小车控制台已启动：%s" % url)
    print("[console] 目标小车 %s:%d（浏览器里可改）" % (host, port))
    sys.stdout.flush()

    if args.connect:
        # 用带前缀的 sid：_touch 只认与自己相等的 owner，
        # 若也叫 "startup"，浏览器里点"连接"（默认 sid=startup）会被当成 owner 续期
        console.connect("startup-autoconnect", host, port)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] 收到 Ctrl+C，正在停止")
    finally:
        server.server_close()
        console.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
