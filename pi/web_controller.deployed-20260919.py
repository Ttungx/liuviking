# -*- coding: utf-8 -*-
"""树莓派手机网页遥控服务（Python 2.7 / 3.x 兼容，仅标准库）。

架构::

    手机浏览器 --HTTP :8082--> 本服务 --TCP :2001--> 原厂 wifirobots.py --GPIO--> 电机

设计要点：
- 不改原厂固件；本服务只是 2001 协议的一个客户端；
- 页面按住方向键发送命令、松开/失焦/切后台立即 STOP；
- 看门狗：超过 SESSION_TIMEOUT 没有页面活动（命令或 /ping）就自动 STOP 并
  断开 2001 连接，把控制权还给官方 APP（原厂 listen(1) 只允许一个客户端）；
- 运动看门狗：按住方向键期间页面每 0.4s 重复刷新命令，超过 MOTION_TIMEOUT
  （默认 1.2s）没刷新就自动 STOP——页面被杀/断网/丢 pointerup 时电机最多
  再跑 1.2s（原厂固件无看门狗，电机保持最后状态，这条是最后的防线）；
- 单控制端：第一个活动的浏览器会话持有控制权，其它会话收到 busy；
- 云台姿态由服务端记录（gimbal_pose.json，含可设定的归位点）并随每次响应返回，
  启动时同步到归位点，页面与外部接口看到的永远是权威值；
  云台走 I2C 直控（实测机械限位水平 0~185、垂直 78~170），
  不受固件 Angle_cal 的 15~160 夹紧；
- 坐标系与 Windows 客户端一致：W 前 S 后 A 左 D 右，协议帧由实机源码确认。

部署（树莓派）::

    cd /home/liuviking/work/wifirobots
    nohup python web_controller.py >> web_controller.log 2>&1 &
    # 手机连热点 wifi-robots.com_* 后打开 http://192.168.1.1:8082

本地调试（Windows）::

    python pi/web_controller.py --port 8082
"""

from __future__ import print_function

import json
import os
import socket
import sys
import threading
import time

try:  # Python 3
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import urlparse, parse_qs
except ImportError:  # Python 2
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn
    from urlparse import urlparse, parse_qs

try:  # 树莓派上的小R定制 smbus，可直接驱动舵机板（云台超出固件范围时用）
    from smbus import SMBus
except ImportError:  # 本地调试无 I2C，云台退化为经固件转发（15~160）
    SMBus = None

# ---------------------------------------------------------------------------
# 协议常量（与 src/protocol.py、实机 wifirobots.py 一致）
# ---------------------------------------------------------------------------

STOP = b"\xff\x00\x00\x00\xff"
HEARTBEAT = b"\xff\xef\xef\xee\xff"
MOTION_KEYS = {
    "w": b"\xff\x00\x01\x00\xff",
    "s": b"\xff\x00\x02\x00\xff",
    "a": b"\xff\x00\x03\x00\xff",
    "d": b"\xff\x00\x04\x00\xff",
}

SPEED_LEFT = 0x01
SPEED_RIGHT = 0x02
# 异常停车后把左右速度恢复到固件默认 100/100（弧线转向会留下不对称速度）
SPEED_DEFAULT = 100

# 云台两轴在协议里的舵机编号（2026-09-18 相机位移实测修正：水平=7 号、垂直=8 号；
# 方向：水平 + 为左转，垂直 - 为上仰）。
# 机械范围同为 2026-09-18 实测（画面位移判限位）：水平 0~188、垂直 ~72~170，取保守值。
# 超出固件 15~160 的范围必须走 I2C 直控——wifirobots.py 的 Angle_cal 会把角度夹紧。
GIMBAL_PAN = 7
GIMBAL_TILT = 8
GIMBAL_AXES = {GIMBAL_PAN: "pan", GIMBAL_TILT: "tilt"}
GIMBAL_LIMITS = {"pan": (0, 185), "tilt": (78, 170)}
SERVO_MIN = 15
SERVO_MAX = 160
GIMBAL_POSE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "gimbal_pose.json"
)


def servo_frame(number, angle):
    """构造 FF 01 <num> <angle> FF 舵机帧。"""
    return (
        b"\xff\x01"
        + bytes(bytearray([int(number)]))
        + bytes(bytearray([int(angle)]))
        + b"\xff"
    )


def speed_frame(side_byte, percent):
    """构造 FF 02 <side> <percent> FF 速度帧。"""
    return (
        b"\xff\x02"
        + bytes(bytearray([side_byte]))
        + bytes(bytearray([max(0, min(100, int(percent)))]))
        + b"\xff"
    )

ACTION_NAMES = {
    "w": "前进",
    "s": "后退",
    "a": "左转",
    "d": "右转",
    "stop": "停止",
}


def packet_hex(data):
    return " ".join("%02X" % byte for byte in bytearray(data))


class BusyError(Exception):
    """另一个浏览器会话正在控制。"""


# ---------------------------------------------------------------------------
# 机器人桥：管理唯一的 2001 长连接 + 看门狗 + 会话所有权
# ---------------------------------------------------------------------------


class RobotBridge(object):
    def __init__(
        self,
        host="127.0.0.1",
        port=2001,
        session_timeout=3.0,
        connect_timeout=1.5,
        heartbeat_interval=10.0,
        motion_timeout=1.2,
    ):
        self.host = host
        self.port = port
        self.session_timeout = session_timeout
        self.connect_timeout = connect_timeout
        self.heartbeat_interval = heartbeat_interval
        # 运动看门狗：方向命令必须由页面按住期间重复刷新，超过该秒数没刷新
        # （页面被杀/断网/丢 pointerup）就自动 STOP，防止电机保持最后状态跑飞。
        self.motion_timeout = motion_timeout

        self._lock = threading.RLock()
        self._sock = None
        self._owner = None
        self._last_activity = 0.0
        self._last_cmd = None
        self._last_error = ""
        self._packets = 0
        self._last_heartbeat = 0.0
        self._t0 = time.time()
        self._last_motion_time = 0.0
        self._gimbal, self._home = self._load_gimbal()

        self._i2c = None
        if SMBus is not None:
            try:
                self._i2c = SMBus(1)
            except Exception as exc:
                print("[web] I2C 初始化失败，云台退化为经固件转发: %s" % exc)
                sys.stdout.flush()
        if self._i2c is not None:
            # 启动即回到归位点，保证"页面显示 = 实际指向"（断电重启后也确定）
            self._gimbal = dict(self._home)
            for number in (GIMBAL_PAN, GIMBAL_TILT):
                try:
                    self._i2c.XiaoRGEEK_SetServo(number, self._gimbal[GIMBAL_AXES[number]])
                except Exception as exc:
                    print("[web] 启动同步云台 %d 号失败: %s" % (number, exc))
                    sys.stdout.flush()
            self._save_gimbal_locked()

        self._stop_event = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop)
        self._watchdog.daemon = True
        self._watchdog.start()

    def _load_gimbal(self):
        """读取持久化的云台姿态与归位点；缺失/损坏时回中位 90/90。"""
        pose = {"pan": 90, "tilt": 90}
        home = {"pan": 90, "tilt": 90}
        try:
            with open(GIMBAL_POSE_FILE) as fh:
                data = json.load(fh)
            for axis in ("pan", "tilt"):
                low, high = GIMBAL_LIMITS[axis]
                pose[axis] = max(low, min(high, int(data.get(axis, 90))))
            saved_home = data.get("home") or {}
            for axis in ("pan", "tilt"):
                low, high = GIMBAL_LIMITS[axis]
                home[axis] = max(low, min(high, int(saved_home.get(axis, 90))))
        except (IOError, ValueError, TypeError):
            pass
        return pose, home

    def _save_gimbal_locked(self):
        try:
            with open(GIMBAL_POSE_FILE, "w") as fh:
                json.dump(
                    {"pan": self._gimbal["pan"], "tilt": self._gimbal["tilt"],
                     "home": self._home},
                    fh,
                )
        except IOError as exc:
            print("[web] 云台姿态持久化失败: %s" % exc)
            sys.stdout.flush()

    def _stamp(self):
        """日志时间戳：服务启动以来的秒数（树莓派无 RTC，只能用相对时间）。"""
        return "[%8.2fs]" % (time.time() - self._t0)

    # -- 内部（调用方需持有锁） -------------------------------------------

    def _connect_locked(self):
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), self.connect_timeout)
        sock.settimeout(2.0)
        self._sock = sock
        self._send_locked(STOP)
        self._last_heartbeat = time.time()
        print("[web] %s 已连接原厂服务 %s:%d，已发 STOP" % (self._stamp(), self.host, self.port))
        sys.stdout.flush()

    def _send_locked(self, packet):
        if self._sock is None:
            return False
        try:
            self._sock.sendall(packet)
        except socket.error as exc:
            self._last_error = str(exc)
            self._close_locked(False)
            return False
        self._packets += 1
        print("[web] %s 发送 %s" % (self._stamp(), packet_hex(packet)))
        sys.stdout.flush()
        return True

    def _close_locked(self, send_stop=True):
        if self._sock is None:
            return
        if send_stop:
            try:
                self._sock.sendall(STOP)
                self._packets += 1
                print("[web] %s 断开前发送 %s" % (self._stamp(), packet_hex(STOP)))
                sys.stdout.flush()
            except socket.error:
                pass
        try:
            self._sock.close()
        except socket.error:
            pass
        self._sock = None
        self._owner = None

    def _touch_locked(self, sid):
        now = time.time()
        if (
            self._owner is not None
            and self._owner != sid
            and now - self._last_activity <= self.session_timeout
        ):
            raise BusyError()
        self._owner = sid
        self._last_activity = now

    # -- 外部 API ----------------------------------------------------------

    def command(self, sid, key):
        """发送方向命令；key 必须属于 w/s/a/d；返回状态字典。"""
        if key not in MOTION_KEYS:
            raise ValueError("invalid key: %r" % (key,))
        with self._lock:
            self._touch_locked(sid)
            now = time.time()
            gap = (now - self._last_motion_time) if self._last_motion_time else -1.0
            print("[web] %s /cmd k=%s sid=%.6s gap=%.2fs" % (self._stamp(), key, sid, gap))
            sys.stdout.flush()
            self._last_motion_time = now
            try:
                self._connect_locked()
            except socket.error as exc:
                self._last_error = str(exc)
                return self._status_locked(ok=False, reason="机器人连接失败: %s" % exc)
            if not self._send_locked(MOTION_KEYS[key]):
                return self._status_locked(ok=False, reason="发送失败")
            self._last_cmd = key
            self._last_activity = time.time()
            return self._status_locked()

    def stop(self, sid=None):
        with self._lock:
            if sid is not None:
                try:
                    self._touch_locked(sid)
                except BusyError:
                    pass  # 任何人都有权急停
            now = time.time()
            held = (now - self._last_motion_time) if self._last_motion_time else -1.0
            print("[web] %s /stop sid=%s held=%.2fs" % (self._stamp(), (sid or "-")[:6], held))
            sys.stdout.flush()
            # 急停必须尽力送达：桥自己有未决运动而连接断了时，重连再送 STOP。
            # 桥没有未决运动时不抢 2001（可能是 GUI/官方 APP 在用），跨客户端急停用"取得控制"。
            if self._sock is None:
                if self._last_cmd is None:
                    self._last_activity = time.time()
                    return self._status_locked()
                try:
                    self._connect_locked()
                except socket.error as exc:
                    self._last_error = str(exc)
                    self._last_cmd = None
                    return self._status_locked(ok=False, reason="急停重连失败: %s" % exc)
            self._send_locked(STOP)
            self._last_cmd = None
            self._last_activity = time.time()
            return self._status_locked()

    def ping(self, sid):
        """心跳：刷新活动时间并返回状态；不触发机器人连接。"""
        with self._lock:
            try:
                self._touch_locked(sid)
            except BusyError:
                status = self._status_locked()
                status["busy"] = True
                return status
            now = time.time()
            if (
                self._sock is not None
                and now - self._last_heartbeat >= self.heartbeat_interval
            ):
                self._send_locked(HEARTBEAT)
                self._last_heartbeat = now
            return self._status_locked()

    def speed(self, sid, side, percent):
        """设置单侧速度（0-100），side 为 left/right；用于诊断与调速。"""
        if side not in ("left", "right"):
            raise ValueError("invalid side: %r" % (side,))
        percent = max(0, min(100, int(percent)))
        side_byte = SPEED_LEFT if side == "left" else SPEED_RIGHT
        with self._lock:
            self._touch_locked(sid)
            print("[web] %s /speed %s=%d sid=%.6s" % (self._stamp(), side, percent, sid))
            sys.stdout.flush()
            try:
                self._connect_locked()
            except socket.error as exc:
                self._last_error = str(exc)
                return self._status_locked(ok=False, reason="机器人连接失败: %s" % exc)
            frame = speed_frame(side_byte, percent)
            if not self._send_locked(frame):
                return self._status_locked(ok=False, reason="发送失败")
            self._last_activity = time.time()
            return self._status_locked()

    def _servo_send_locked(self, number, angle):
        """经 I2C 或固件发送单路舵机（调用方需持有锁）；成功返回 None，否则返回原因。"""
        if self._i2c is not None:
            try:
                self._i2c.XiaoRGEEK_SetServo(number, angle)
            except Exception as exc:
                return "舵机板 I2C 写入失败: %s" % exc
            return None
        try:
            self._connect_locked()
        except socket.error as exc:
            return "机器人连接失败: %s" % exc
        if not self._send_locked(servo_frame(number, angle)):
            return "发送失败"
        return None

    def servo(self, sid, number, angle):
        """舵机控制：1-8 号；云台两轴走 I2C 直控（实测机械范围），其余沿用固件 15-160。"""
        number = int(number)
        if not 1 <= number <= 8:
            raise ValueError("invalid servo number: %r" % (number,))
        axis = GIMBAL_AXES.get(number)
        if axis is not None and self._i2c is not None:
            low, high = GIMBAL_LIMITS[axis]
        else:
            low, high = SERVO_MIN, SERVO_MAX
        angle = max(low, min(high, int(angle)))
        with self._lock:
            self._touch_locked(sid)
            print("[web] %s /servo n=%d a=%d sid=%.6s" % (self._stamp(), number, angle, sid))
            sys.stdout.flush()
            err = self._servo_send_locked(number, angle)
            if err is not None:
                self._last_error = err
                return self._status_locked(ok=False, reason=err)
            if axis is not None:
                self._gimbal[axis] = angle
                self._save_gimbal_locked()
            self._last_activity = time.time()
            return self._status_locked()

    def gimbal_set(self, sid, pan, tilt):
        """同时设定云台两轴（无级/组合控制用），走同一套限位与记录。"""
        pan = max(GIMBAL_LIMITS["pan"][0], min(GIMBAL_LIMITS["pan"][1], int(pan)))
        tilt = max(GIMBAL_LIMITS["tilt"][0], min(GIMBAL_LIMITS["tilt"][1], int(tilt)))
        with self._lock:
            self._touch_locked(sid)
            print("[web] %s /gimbal_set pan=%d tilt=%d sid=%.6s"
                  % (self._stamp(), pan, tilt, sid))
            sys.stdout.flush()
            for number, angle in ((GIMBAL_PAN, pan), (GIMBAL_TILT, tilt)):
                err = self._servo_send_locked(number, angle)
                if err is not None:
                    self._last_error = err
                    return self._status_locked(ok=False, reason=err)
            self._gimbal = {"pan": pan, "tilt": tilt}
            self._save_gimbal_locked()
            self._last_activity = time.time()
            return self._status_locked()

    def set_home(self):
        """把当前姿态记为归位点并持久化（仅记录不动舵机，任何客户端可调）。"""
        with self._lock:
            self._home = dict(self._gimbal)
            self._save_gimbal_locked()
            print("[web] %s 归位点已设为 pan=%d tilt=%d"
                  % (self._stamp(), self._home["pan"], self._home["tilt"]))
            sys.stdout.flush()
            return self._status_locked()

    def claim(self, sid):
        """显式取得控制权：当前无运动指令时允许抢控，方向键仍被按住时拒绝。"""
        with self._lock:
            if (
                self._owner is not None
                and self._owner != sid
                and self._last_cmd is not None
            ):
                status = self._status_locked(ok=False, reason="busy")
                status["busy"] = True
                return status
            if self._sock is not None:
                self._send_locked(STOP)
            self._owner = sid
            self._last_activity = time.time()
            self._last_cmd = None
            print("[web] %s /claim sid=%.6s 取得控制权" % (self._stamp(), sid))
            sys.stdout.flush()
            return self._status_locked()

    def status(self):
        with self._lock:
            return self._status_locked()

    def gimbal(self):
        """返回服务端权威的云台姿态、归位点与实测机械限位。"""
        with self._lock:
            payload = dict(self._gimbal)
            payload["home"] = dict(self._home)
        payload["ok"] = True
        payload["limits"] = dict((k, list(v)) for k, v in GIMBAL_LIMITS.items())
        return payload

    def close(self):
        self._stop_event.set()
        with self._lock:
            self._close_locked(True)

    def _status_locked(self, ok=True, reason=""):
        state = "connected" if self._sock is not None else "disconnected"
        return {
            "ok": ok,
            "reason": reason or self._last_error,
            "robot": state,
            "last": self._last_cmd,
            "packets": self._packets,
            "busy": False,
            "gimbal": dict(self._gimbal),
            "home": dict(self._home),
        }

    # -- 看门狗 ------------------------------------------------------------

    def _watchdog_loop(self):
        """页面无活动超时 -> STOP + 断开，把 2001 让给官方 APP。"""
        while not self._stop_event.wait(0.5):
            with self._lock:
                if self._sock is None:
                    continue
                now = time.time()
                # 运动看门狗：按住运动时页面每 0.4s 重复 /cmd 刷新；
                # 超时没刷新说明页面已死/断网/事件丢失，立刻 STOP。
                # 弧线转向会留下不对称速度，这里一并恢复默认，保证下一次直行是直的。
                if (
                    self._last_cmd in MOTION_KEYS
                    and now - self._last_motion_time > self.motion_timeout
                ):
                    print(
                        "[web] %s 运动超时 %.1fs 无刷新，自动 STOP 并恢复默认速度"
                        % (self._stamp(), now - self._last_motion_time)
                    )
                    sys.stdout.flush()
                    self._send_locked(STOP)
                    self._send_locked(speed_frame(SPEED_LEFT, SPEED_DEFAULT))
                    self._send_locked(speed_frame(SPEED_RIGHT, SPEED_DEFAULT))
                    self._last_cmd = None
                    continue
                if now - self._last_activity > self.session_timeout:
                    print("[web] %s 超时无活动，自动 STOP 并断开" % self._stamp())
                    sys.stdout.flush()
                    self._last_cmd = None
                    self._close_locked(True)


# ---------------------------------------------------------------------------
# 手机页面
# ---------------------------------------------------------------------------

PAGE = u"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>小车遥控</title>
<style>
:root{--bg:#101418;--panel:#1b232b;--key:#2b3846;--keyon:#3d6ea8;--stop:#c62828;--ok:#43a047;--warn:#f9a825;--off:#9e9e9e;--text:#e8eef5}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;overscroll-behavior:none;touch-action:manipulation}
body{display:flex;flex-direction:column;padding:8px 10px calc(8px + env(safe-area-inset-bottom));gap:8px;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;max-width:720px;margin:0 auto}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px}
.status{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--off);min-width:0}
.dot{width:10px;height:10px;border-radius:50%;background:var(--off);flex:none}
.dot.on{background:var(--ok)}.dot.busy{background:var(--warn)}
.pose{color:var(--text);font-variant-numeric:tabular-nums;white-space:nowrap}
.topBtns{display:flex;gap:8px;flex:none}
.tbtn{appearance:none;border:none;border-radius:10px;background:var(--panel);color:var(--text);font-size:14px;padding:8px 14px}
.tbtn.danger{background:var(--stop);font-weight:700}
#videoBack{display:none}
body.driving #videoBtn{display:none}
body.driving #videoBack{display:inline-block}
main{display:grid;grid-template-columns:1fr 1fr;gap:8px;align-items:start}
.card{background:var(--panel);border-radius:14px;padding:8px;display:flex;flex-direction:column;gap:8px;min-width:0}
.cardTitle{font-size:12px;font-weight:700;color:#8fa0b2}
.dpad{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.key{appearance:none;border:none;border-radius:12px;background:var(--key);color:var(--text);font-size:26px;font-weight:700;display:flex;align-items:center;justify-content:center;touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;aspect-ratio:1/1;min-width:0;padding:0}
.key:active,.key.on{background:var(--keyon)}
.key.stop{grid-column:2;grid-row:2;background:var(--stop);font-size:13px}
#kw{grid-column:2;grid-row:1}#ks{grid-column:2;grid-row:3}#ka{grid-column:1;grid-row:2}#kd{grid-column:3;grid-row:2}
.gpad{position:relative;height:150px;border-radius:10px;background:#101418;touch-action:none;overflow:hidden}
.gknob{position:absolute;width:30px;height:30px;border-radius:50%;background:var(--keyon);transform:translate(-50%,-50%);pointer-events:none}
.nudge{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
.nudge button{appearance:none;border:none;border-radius:10px;background:var(--key);color:var(--text);font-size:15px;padding:9px 0;min-width:0}
.nudge button:active{background:var(--keyon)}
#gimbalUp{grid-column:2;grid-row:1}#gimbalLeft{grid-column:1;grid-row:2}#gimbalHome{grid-column:2;grid-row:2}#gimbalRight{grid-column:3;grid-row:2}#gimbalDown{grid-column:2;grid-row:3}
#gimbalHome{font-size:12px;background:var(--panel);border:1px solid var(--key)}
.more{background:var(--panel);border-radius:14px;padding:0 10px 10px}
.more summary{padding:10px 0;font-size:13px;color:#8fa0b2;cursor:pointer;outline:none}
.more .gimbal{background:transparent;padding:0 0 6px}
.gimbal{display:flex;flex-direction:column;gap:6px}
.gimbalRow{display:flex;align-items:center;gap:8px;font-size:13px}
.gimbalRow .glabel{width:34px;color:#8fa0b2}
.gimbalRow input[type=range]{flex:1;min-width:0;accent-color:#3d6ea8}
.gimbalRow .stepBtn{appearance:none;border:none;border-radius:8px;background:var(--key);color:var(--text);font-size:16px;width:36px;height:30px;padding:0;flex:none}
.gimbalVal{width:46px;text-align:right;font-variant-numeric:tabular-nums}
.ghomeRow{display:flex;align-items:center;justify-content:space-between;font-size:13px;color:#8fa0b2}
.ghomeRow .stepBtn{width:auto;padding:0 12px}
#videoBox{display:none}
body.driving{overflow:hidden}
body.driving #videoBox{display:block;position:fixed;inset:0;z-index:0;background:#000}
body.driving #video{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
body.driving .top{position:fixed;top:0;left:0;right:0;z-index:2;background:rgba(0,0,0,.5);padding:6px 10px}
body.driving main{position:fixed;left:0;right:0;bottom:0;z-index:2;background:linear-gradient(transparent,rgba(0,0,0,.65));padding:8px 10px calc(8px + env(safe-area-inset-bottom))}
body.driving .card{background:rgba(27,35,43,.78)}
body.driving .cardTitle{display:none}
body.driving .gpad{height:110px}
body.driving .key{font-size:20px}
body.driving .more{display:none}
#videoHint{position:absolute;left:0;right:0;top:50%;text-align:center;color:#777;font-size:14px}
</style>
</head>
<body>
<header class="top">
  <div class="status"><span id="dot" class="dot"></span><span id="statusText">连接中…</span><span id="poseText" class="pose">H:90° V:90°</span></div>
  <div class="topBtns"><button class="tbtn" id="videoBtn">视频</button><button class="tbtn danger" id="stopAll">急停</button><button class="tbtn" id="videoBack">退出</button></div>
</header>
<main>
  <section class="card">
    <div class="cardTitle">小车 · WASD</div>
    <div class="dpad">
      <button class="key" id="kw">▲</button>
      <button class="key" id="ka">◀</button>
      <button class="key stop" id="stop">STOP</button>
      <button class="key" id="kd">▶</button>
      <button class="key" id="ks">▼</button>
    </div>
  </section>
  <section class="card">
    <div class="cardTitle">云台 · 方向键</div>
    <div class="gpad" id="gimbalPad"><div class="gknob" id="gimbalKnob"></div></div>
    <div class="nudge">
      <button id="gimbalUp">▲</button>
      <button id="gimbalLeft">◀</button>
      <button id="gimbalHome">归位</button>
      <button id="gimbalRight">▶</button>
      <button id="gimbalDown">▼</button>
    </div>
  </section>
</main>
<details class="more"><summary>精调 · 归位点</summary>
  <div class="gimbal">
    <div class="gimbalRow"><span class="glabel">水平</span><button class="stepBtn" id="panMinus">−</button><input id="panRange" type="range" min="0" max="185" step="1" value="90"><button class="stepBtn" id="panPlus">+</button><span class="gimbalVal" id="panVal">90°</span></div>
    <div class="gimbalRow"><span class="glabel">垂直</span><button class="stepBtn" id="tiltMinus">−</button><input id="tiltRange" type="range" min="78" max="170" step="1" value="90"><button class="stepBtn" id="tiltPlus">+</button><span class="gimbalVal" id="tiltVal">90°</span></div>
  </div>
  <div class="ghomeRow"><span id="homeVal">归位点 H:90° V:90°</span><button class="stepBtn" id="setHome">设为归位</button></div>
</details>
<div id="videoBox">
  <img id="video" alt="">
  <div id="videoHint">视频未连接（摄像头未接时正常）</div>
</div>
<script>
(function(){
"use strict";
var SID = Math.random().toString(36).slice(2) + Date.now().toString(36);
var activeKey = null;
var busy = false;
var motionPointer = null;
var stopSeq = 0;         /* 每次 /stop 自增；用于识别"迟到的 /cmd"并补偿停车 */
var arcMode = false;     /* 默认原地转向（已验证稳定）；弧线差速待实车验证后再开启 */
var ARC_INNER = 30;      /* 弧线内侧轮速度百分比（可按需调整 15~50） */
var ARC_OUTER = 80;      /* 弧线外侧轮速度百分比 */

function api(path){
  return fetch(path + (path.indexOf("?")<0?"?":"&") + "sid=" + SID, {cache:"no-store"})
    .then(function(r){ return r.json(); })
    .catch(function(){ return null; });
}
function setStatus(data){
  if(!data){ return; }
  applyGimbal(data);
  busy = !!data.busy;
  var dot = document.getElementById("dot");
  var text = document.getElementById("statusText");
  if(busy){ dot.className="dot busy"; text.textContent="其它设备控制中"; return; }
  if(data.robot==="connected"){ dot.className="dot on"; text.textContent="已连接"; }
  else { dot.className="dot"; text.textContent="未连接"; }
}
function sendKey(k){
  activeKey = k;
  var stopAtDispatch = stopSeq;   /* 记录发出时刻的停止代数 */
  var dir = k;
  var chain;
  if(arcMode && (k === "a" || k === "d")){
    /* 弧线转向：双轮都朝前（前进命令），内侧轮减速、外侧轮全速 */
    var innerSide = (k === "a") ? "left" : "right";
    var outerSide = (k === "a") ? "right" : "left";
    dir = "w";
    chain = api("/speed?side=" + innerSide + "&value=" + ARC_INNER)
      .then(function(){ return api("/speed?side=" + outerSide + "&value=" + ARC_OUTER); });
  } else {
    /* 直行/原地转向：先恢复对称速度，防上一次弧线留下不对称 */
    chain = api("/speed?side=left&value=100").then(function(){ return api("/speed?side=right&value=100"); });
  }
  chain.then(function(){ return api("/cmd?k=" + dir); })
    .then(function(d){
      if(!d || !d.ok){ stopLocal(d && d.reason || "命令失败"); return; }
      setStatus(d);
      /* 竞态补偿：若本条 /cmd 在路上时发生过 /stop（松手/急停/切后台），
         它会比 /stop 晚到并把电机重新锁存——立刻补一条 /stop 停车 */
      if(stopSeq !== stopAtDispatch){ api("/stop"); }
    });
}
function stopLocal(reason){
  stopSeq++;
  motionPointer = null;
  if(activeKey !== null || reason){
    activeKey = null;
    api("/stop").then(setStatus);
  } else {
    api("/stop").then(setStatus);
  }
}
function bindHold(el, k){
  var downAt = 0, timer = null;
  var repeat = function(){
    if(activeKey === k){ sendKey(k); }
    else if(timer){ clearInterval(timer); timer = null; }
  };
  el.addEventListener("pointerdown", function(ev){
    ev.preventDefault();
    el.classList.add("on");
    downAt = Date.now();
    motionPointer = ev.pointerId;
    try{ if(el.setPointerCapture){ el.setPointerCapture(ev.pointerId); } }catch(e){}
    sendKey(k);
    if(timer){ clearInterval(timer); }
    timer = setInterval(repeat, 400);
  });
  el.addEventListener("touchstart", function(ev){ ev.preventDefault(); }, {passive:false});
  var end = function(ev, name){
    if(ev){ ev.preventDefault(); }
    if(!downAt){ return; }
    var held = ((Date.now() - downAt) / 1000).toFixed(2);
    downAt = 0;
    el.classList.remove("on");
    if(timer){ clearInterval(timer); timer = null; }
    api("/diag?ev=" + name + "&k=" + k + "&held=" + held);
    if(activeKey===k){ stopLocal(); }
  };
  el.addEventListener("pointerup", function(ev){ end(ev, "up"); });
  el.addEventListener("pointercancel", function(ev){ end(ev, "cancel"); });
  el.addEventListener("contextmenu", function(ev){ ev.preventDefault(); });
}
["w","a","s","d"].forEach(function(k){ bindHold(document.getElementById("k"+k), k); });
(function(){
  var globalStop = function(ev){
    /* 只兜底"触摸按下过"的运动（motionPointer 记录了按下的指针），
       键盘遥控不受鼠标 pointerup 影响 */
    if(activeKey !== null && motionPointer !== null && ev.pointerId === motionPointer){
      motionPointer = null;
      stopLocal();
    }
  };
  window.addEventListener("pointerup", globalStop, true);
  window.addEventListener("pointercancel", globalStop, true);
  if(!window.PointerEvent){
    window.addEventListener("touchend", function(){ if(activeKey !== null){ stopLocal(); } }, true);
  }
})();
document.getElementById("stop").addEventListener("pointerdown", function(ev){ ev.preventDefault(); stopLocal(); });
document.getElementById("stopAll").addEventListener("click", function(){ stopLocal(); });

/* 云台两轴：水平=协议 7 号、垂直=协议 8 号（2026-09-18 相机位移实测修正；
   水平 + 为左转，垂直 - 为上仰）。姿态与归位点由服务端记录并随每次响应返回，
   页面只显示服务端的权威值；下方触控板为无级调节（拖动即转到对应姿态）。 */
var GIMBAL_PAN = 7, GIMBAL_TILT = 8;
var gimbalPose = {pan: 90, tilt: 90};
var homePose = {pan: 90, tilt: 90};
var panLim = [0, 185], tiltLim = [78, 170];
var pad = document.getElementById("gimbalPad");
var knob = document.getElementById("gimbalKnob");
var homeVal = document.getElementById("homeVal");
var gimbalSliding = 0, slideTimer = null, slidePending = null, slideRelease = null;
var panRange = document.getElementById("panRange");
var tiltRange = document.getElementById("tiltRange");
var panVal = document.getElementById("panVal");
var tiltVal = document.getElementById("tiltVal");
var poseText = document.getElementById("poseText");
function renderPad(){
  var px = (panLim[1] - gimbalPose.pan) / (panLim[1] - panLim[0]);
  var py = (gimbalPose.tilt - tiltLim[0]) / (tiltLim[1] - tiltLim[0]);
  knob.style.left = (px * 100) + "%";
  knob.style.top = (py * 100) + "%";
}
function renderGimbal(skipSlider){
  if(!skipSlider){ panRange.value = gimbalPose.pan; tiltRange.value = gimbalPose.tilt; }
  panVal.textContent = gimbalPose.pan + "°";
  tiltVal.textContent = gimbalPose.tilt + "°";
  poseText.textContent = "H:" + gimbalPose.pan + "° V:" + gimbalPose.tilt + "°";
  renderPad();
}
function renderHome(){
  homeVal.textContent = "归位点 H:" + homePose.pan + "° V:" + homePose.tilt + "°";
}
function applyGimbal(data){
  if(!data || !data.gimbal){ return; }
  gimbalPose.pan = data.gimbal.pan;
  gimbalPose.tilt = data.gimbal.tilt;
  if(data.home){ homePose = data.home; renderHome(); }
  renderGimbal(gimbalSliding);
}
function applyLimits(data){
  if(!data || !data.limits){ return; }
  panLim = data.limits.pan; tiltLim = data.limits.tilt;
  panRange.min = panLim[0]; panRange.max = panLim[1];
  tiltRange.min = tiltLim[0]; tiltRange.max = tiltLim[1];
}
function sendAxis(axis, value){
  var lim = (axis === "pan") ? panLim : tiltLim;
  value = Math.max(lim[0], Math.min(lim[1], Math.round(value)));
  gimbalPose[axis] = value;
  renderGimbal(gimbalSliding);
  var n = (axis === "pan") ? GIMBAL_PAN : GIMBAL_TILT;
  api("/servo?n=" + n + "&a=" + value).then(applyGimbal);
}
function flushSlide(){
  if(slideTimer){ clearTimeout(slideTimer); slideTimer = null; }
  if(slidePending){ var p = slidePending; slidePending = null; sendAxis(p[0], p[1]); }
}
function slideAxis(axis, value){
  gimbalSliding++;
  gimbalPose[axis] = Math.round(value);
  renderGimbal(true);
  slidePending = [axis, value];
  if(!slideTimer){ slideTimer = setTimeout(flushSlide, 120); }
  if(slideRelease){ clearTimeout(slideRelease); }
  slideRelease = setTimeout(function(){ gimbalSliding = 0; }, 600);
}
function bindSlider(el, axis){
  el.addEventListener("input", function(){ slideAxis(axis, +this.value); });
  el.addEventListener("change", function(){ flushSlide(); });
}
bindSlider(panRange, "pan");
bindSlider(tiltRange, "tilt");
function servoStep(n, delta){
  var axis = (n === GIMBAL_PAN) ? "pan" : "tilt";
  sendAxis(axis, gimbalPose[axis] + delta);
}
function bindServo(el, n, delta){
  var timer = null;
  var step = function(){ servoStep(n, delta); };
  el.addEventListener("pointerdown", function(ev){
    ev.preventDefault();
    try{ if(el.setPointerCapture){ el.setPointerCapture(ev.pointerId); } }catch(e){}
    step();
    if(timer){ clearInterval(timer); }
    timer = setInterval(step, 220);
  });
  var stop = function(ev){ if(ev){ ev.preventDefault(); } if(timer){ clearInterval(timer); timer = null; } };
  el.addEventListener("pointerup", stop);
  el.addEventListener("pointercancel", stop);
  el.addEventListener("touchstart", function(ev){ ev.preventDefault(); }, {passive:false});
  el.addEventListener("contextmenu", function(ev){ ev.preventDefault(); });
}
bindServo(document.getElementById("gimbalUp"), GIMBAL_TILT, -10);
bindServo(document.getElementById("gimbalDown"), GIMBAL_TILT, 10);
bindServo(document.getElementById("gimbalLeft"), GIMBAL_PAN, 15);
bindServo(document.getElementById("gimbalRight"), GIMBAL_PAN, -15);
document.getElementById("gimbalHome").addEventListener("click", function(){
  api("/gimbal_set?pan=" + homePose.pan + "&tilt=" + homePose.tilt).then(applyGimbal);
});
document.getElementById("setHome").addEventListener("click", function(){
  api("/gimbal_home").then(applyGimbal);
});
var padActive = false, padTimer = null, padPending = null;
function padPose(clientX, clientY){
  var r = pad.getBoundingClientRect();
  var x = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
  var y = Math.max(0, Math.min(1, (clientY - r.top) / r.height));
  return {pan: Math.round(panLim[1] - x * (panLim[1] - panLim[0])),
          tilt: Math.round(tiltLim[0] + y * (tiltLim[1] - tiltLim[0]))};
}
function padFlush(){
  if(padTimer){ clearTimeout(padTimer); padTimer = null; }
  if(padPending){
    var t = padPending; padPending = null;
    api("/gimbal_set?pan=" + t.pan + "&tilt=" + t.tilt).then(applyGimbal);
  }
}
function padMove(clientX, clientY){
  var t = padPose(clientX, clientY);
  gimbalPose.pan = t.pan; gimbalPose.tilt = t.tilt;
  renderGimbal(true);
  padPending = t;
  if(!padTimer){ padTimer = setTimeout(padFlush, 110); }
}
pad.addEventListener("pointerdown", function(ev){
  ev.preventDefault(); padActive = true;
  try{ if(pad.setPointerCapture){ pad.setPointerCapture(ev.pointerId); } }catch(e){}
  padMove(ev.clientX, ev.clientY);
});
pad.addEventListener("pointermove", function(ev){
  if(padActive){ ev.preventDefault(); padMove(ev.clientX, ev.clientY); }
});
var padEnd = function(){ if(padActive){ padActive = false; padFlush(); } };
pad.addEventListener("pointerup", padEnd);
pad.addEventListener("pointercancel", padEnd);
document.getElementById("panMinus").addEventListener("click", function(){ sendAxis("pan", gimbalPose.pan - 1); });
document.getElementById("panPlus").addEventListener("click", function(){ sendAxis("pan", gimbalPose.pan + 1); });
document.getElementById("tiltMinus").addEventListener("click", function(){ sendAxis("tilt", gimbalPose.tilt - 1); });
document.getElementById("tiltPlus").addEventListener("click", function(){ sendAxis("tilt", gimbalPose.tilt + 1); });
renderGimbal();
renderHome();
api("/gimbal").then(function(d){ applyLimits(d); applyGimbal(d); });

document.addEventListener("visibilitychange", function(){ if(document.hidden){ stopLocal(); } });
window.addEventListener("blur", function(){ stopLocal(); });
window.addEventListener("keydown", function(ev){
  var k = (ev.key||"").toLowerCase();
  if(k===" " || k==="spacebar"){ ev.preventDefault(); stopLocal(); return; }
  if("wasd".indexOf(k)>=0){
    ev.preventDefault();
    var el=document.getElementById("k"+k); el.classList.add("on");
    sendKey(k);  /* OS 按键重复也作为心跳刷新，防止按键卡死跑飞 */
  }
  /* 方向键控制云台（驾驶模式主力）：上=仰/下=俯/左=左转/右=右转，按住连发 */
  if(ev.key==="ArrowUp"){ ev.preventDefault(); sendAxis("tilt", gimbalPose.tilt - 10); }
  else if(ev.key==="ArrowDown"){ ev.preventDefault(); sendAxis("tilt", gimbalPose.tilt + 10); }
  else if(ev.key==="ArrowLeft"){ ev.preventDefault(); sendAxis("pan", gimbalPose.pan + 15); }
  else if(ev.key==="ArrowRight"){ ev.preventDefault(); sendAxis("pan", gimbalPose.pan - 15); }
});
window.addEventListener("keyup", function(ev){
  var k = (ev.key||"").toLowerCase();
  if("wasd".indexOf(k)>=0){ ev.preventDefault(); var el=document.getElementById("k"+k); el.classList.remove("on"); if(activeKey===k){ stopLocal(); } }
});
setInterval(function(){ api("/ping").then(setStatus); }, 1000);
api("/ping").then(setStatus);
api("/claim").then(function(d){ if(d){ setStatus(d); } });

var videoBox = document.getElementById("videoBox");
var videoImg = document.getElementById("video");
var videoHint = document.getElementById("videoHint");
var videoFailed = false;
function showVideoHint(text){
  if(text){ videoHint.textContent = text; }
  videoHint.style.display = "block";
}
function hideVideoHint(){ videoHint.style.display = "none"; }
/* MJPEG 是无限流，部分浏览器不触发 load 事件；用 naturalWidth>0 判定第一帧已出图 */
function checkVideoAlive(){
  if(videoFailed){ return; }
  if(videoImg.getAttribute("src") && videoImg.naturalWidth > 0){ hideVideoHint(); }
}
videoImg.addEventListener("load", checkVideoAlive);
setInterval(checkVideoAlive, 1000);
document.getElementById("videoBtn").addEventListener("click", function(){
  /* 每次点开都重设 src：流中断后（error 后 src 仍在）可强制重连 */
  videoImg.setAttribute("src", "http://" + location.hostname + ":8080/?action=stream");
  videoFailed = false;
  showVideoHint("正在连接视频…");
  document.body.classList.add("driving");
});
document.getElementById("videoBack").addEventListener("click", function(){
  videoImg.removeAttribute("src");
  document.body.classList.remove("driving");
  showVideoHint("视频未连接（摄像头未接时正常）");
});
videoImg.addEventListener("error", function(){
  if(!videoImg.getAttribute("src")){ return; }
  videoFailed = true;
  showVideoHint("视频不可用（摄像头未接或未启动，点“视频”重试）");
});
})();
</script>
</body>
</html>
"""


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class HandlerBase(BaseHTTPRequestHandler):
    bridge = None  # 由 make_handler / 子类注入

    protocol_version = "HTTP/1.1"

    def _respond(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _respond_json(self, payload, status=200):
        self._respond(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _respond_busy(self):
        payload = self.bridge.status()
        payload["busy"] = True
        payload["ok"] = False
        payload["reason"] = "busy"
        self._respond_json(payload, 423)

    def _query(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        sid = (params.get("sid") or [""])[0]
        return parsed.path, params, sid

    def do_GET(self):  # noqa: N802
        path, params, sid = self._query()
        try:
            if path in ("/", "/index.html"):
                self._respond(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/cmd":
                key = (params.get("k") or [""])[0].lower()
                try:
                    self._respond_json(self.bridge.command(sid, key))
                except BusyError:
                    self._respond_busy()
                except ValueError:
                    self._respond_json({"ok": False, "reason": "invalid key"}, 400)
            elif path == "/speed":
                side = (params.get("side") or [""])[0].lower()
                try:
                    value = int((params.get("value") or ["100"])[0])
                except ValueError:
                    value = 100
                try:
                    self._respond_json(self.bridge.speed(sid, side, value))
                except BusyError:
                    self._respond_busy()
                except ValueError:
                    self._respond_json({"ok": False, "reason": "invalid side"}, 400)
            elif path == "/servo":
                try:
                    number = int((params.get("n") or ["1"])[0])
                    angle = int((params.get("a") or ["90"])[0])
                except ValueError:
                    self._respond_json({"ok": False, "reason": "invalid servo args"}, 400)
                    return
                try:
                    self._respond_json(self.bridge.servo(sid, number, angle))
                except BusyError:
                    self._respond_busy()
                except ValueError:
                    self._respond_json({"ok": False, "reason": "invalid servo number"}, 400)
            elif path == "/claim":
                self._respond_json(self.bridge.claim(sid))
            elif path == "/stop":
                self._respond_json(self.bridge.stop(sid))
            elif path == "/ping":
                self._respond_json(self.bridge.ping(sid))
            elif path == "/status":
                self._respond_json(self.bridge.status())
            elif path == "/gimbal":
                self._respond_json(self.bridge.gimbal())
            elif path == "/gimbal_set":
                try:
                    pan = int((params.get("pan") or [""])[0])
                    tilt = int((params.get("tilt") or [""])[0])
                except ValueError:
                    self._respond_json({"ok": False, "reason": "invalid gimbal args"}, 400)
                    return
                try:
                    self._respond_json(self.bridge.gimbal_set(sid, pan, tilt))
                except BusyError:
                    self._respond_busy()
            elif path == "/gimbal_home":
                self._respond_json(self.bridge.set_home())
            elif path == "/diag":
                ev = (params.get("ev") or [""])[0]
                k = (params.get("k") or [""])[0]
                held = (params.get("held") or [""])[0]
                print("[web] %s /diag ev=%s k=%s held=%ss" % (self.bridge._stamp(), ev, k, held))
                sys.stdout.flush()
                self._respond_json({"ok": True})
            elif path == "/favicon.ico":
                self._respond(204, b"", "image/x-icon")
            else:
                self._respond_json({"ok": False, "reason": "not found"}, 404)
        except Exception as exc:  # pragma: no cover - 防单个请求拖垮服务
            try:
                self._respond_json({"ok": False, "reason": str(exc)}, 500)
            except Exception:
                pass

    def do_POST(self):  # noqa: N802
        self.do_GET()

    def handle(self):
        try:
            BaseHTTPRequestHandler.handle(self)
        except socket.error:
            pass  # 客户端(浏览器/手机)中途断开，静默忽略

    def log_message(self, fmt, *args):  # 静默访问日志
        return


def make_server(bridge, bind="0.0.0.0", port=8082):
    """构造 ThreadingHTTPServer（测试和部署共用）。"""

    class Handler(HandlerBase):
        pass

    Handler.bridge = bridge  # 兼容 Python 2 经典类，不能用 type() 动态建类

    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return ThreadingHTTPServer((bind, port), Handler)


def build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(description="树莓派手机网页遥控服务")
    parser.add_argument("--bind", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8082, help="HTTP 端口，默认 8082")
    parser.add_argument("--robot-host", default="127.0.0.1", help="原厂控制服务地址")
    parser.add_argument("--robot-port", type=int, default=2001, help="原厂控制端口")
    parser.add_argument("--session-timeout", type=float, default=3.0, help="页面活动超时（秒）后自动 STOP")
    parser.add_argument(
        "--motion-timeout",
        type=float,
        default=1.2,
        help="运动命令刷新超时（秒）后自动 STOP（页面按住期间每 0.4s 刷新一次）",
    )
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    bridge = RobotBridge(
        host=args.robot_host,
        port=args.robot_port,
        session_timeout=args.session_timeout,
        motion_timeout=args.motion_timeout,
    )
    server = make_server(bridge, args.bind, args.port)
    print("[web] 手机遥控服务已启动 http://0.0.0.0:%d/" % args.port)
    print("[web] 手机连热点 wifi-robots.com_* 后访问 http://192.168.1.1:%d/" % args.port)
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
