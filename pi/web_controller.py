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

        self._stop_event = threading.Event()
        self._watchdog = threading.Thread(target=self._watchdog_loop)
        self._watchdog.daemon = True
        self._watchdog.start()

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

    def servo(self, sid, number, angle):
        """舵机控制：number 1-8，angle 15-160（与固件 Angle_cal 一致）。"""
        number = int(number)
        if not 1 <= number <= 8:
            raise ValueError("invalid servo number: %r" % (number,))
        angle = max(15, min(160, int(angle)))
        with self._lock:
            self._touch_locked(sid)
            print("[web] %s /servo n=%d a=%d sid=%.6s" % (self._stamp(), number, angle, sid))
            sys.stdout.flush()
            try:
                self._connect_locked()
            except socket.error as exc:
                self._last_error = str(exc)
                return self._status_locked(ok=False, reason="机器人连接失败: %s" % exc)
            frame = (
                b"\xff\x01"
                + bytes(bytearray([number]))
                + bytes(bytearray([angle]))
                + b"\xff"
            )
            if not self._send_locked(frame):
                return self._status_locked(ok=False, reason="发送失败")
            self._last_activity = time.time()
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
body{display:flex;flex-direction:column;padding:10px 12px calc(10px + env(safe-area-inset-bottom));gap:8px;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
header{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:14px}
.brand{font-weight:700}
.status{display:flex;align-items:center;gap:6px;color:var(--off)}
.dot{width:10px;height:10px;border-radius:50%;background:var(--off)}
.dot.on{background:var(--ok)}.dot.busy{background:var(--warn)}
.msg{font-size:12px;color:#8fa0b2;min-height:16px}
.pad{flex:1;display:grid;grid-template-columns:repeat(3,1fr);grid-template-rows:repeat(3,1fr);gap:10px;min-height:320px}
.key{appearance:none;border:none;border-radius:16px;background:var(--key);color:var(--text);font-size:34px;font-weight:700;display:flex;align-items:center;justify-content:center;touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
.key:active,.key.on{background:var(--keyon)}
.key.stop{grid-column:2;grid-row:2;background:var(--stop);font-size:20px}
#kw{grid-column:2;grid-row:1}#ks{grid-column:2;grid-row:3}#ka{grid-column:1;grid-row:2}#kd{grid-column:3;grid-row:2}
.bar{display:flex;gap:10px}
.bar button{flex:1;appearance:none;border:none;border-radius:12px;background:var(--panel);color:var(--text);font-size:16px;padding:14px 8px}
.bar button.active{background:var(--keyon)}
#videoBox{position:fixed;inset:0;background:#000;display:none;flex-direction:column;z-index:9}
#videoBox.show{display:flex}
#videoBox header{background:#000;padding:4px 8px}
#video{flex:1;object-fit:contain;width:100%;min-height:0}
#videoHint{position:absolute;left:0;right:0;top:50%;text-align:center;color:#777;font-size:14px}
</style>
</head>
<body>
<header>
  <div class="brand">小车遥控</div>
  <div class="status"><span id="dot" class="dot"></span><span id="statusText">连接中…</span></div>
</header>
<div id="msg" class="msg">按住方向键移动，松开立即停止</div>
<div class="pad">
  <button class="key" id="kw" data-k="w">▲</button>
  <button class="key" id="ka" data-k="a">◀</button>
  <button class="key stop" id="stop">STOP</button>
  <button class="key" id="kd" data-k="d">▶</button>
  <button class="key" id="ks" data-k="s">▼</button>
</div>
<div class="bar">
  <button id="videoBtn">视频</button>
  <button id="stopAll">急停</button>
</div>
<div class="bar">
  <button id="gimbalUp">云台上</button>
  <button id="gimbalDown">云台下</button>
  <button id="gimbalLeft">云台左</button>
  <button id="gimbalRight">云台右</button>
  <button id="gimbalHome">归位</button>
</div>
<div id="videoBox">
  <header><button id="videoBack" class="key" style="font-size:14px;padding:6px 12px">返回</button></header>
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

/* 云台舵机通道：2026-09-13 实车四方向实测最终确认——水平轴在协议 8 号、垂直轴在
   协议 7 号（两颗舵机插头与出厂约定交叉，实测两通道均正常，按实际接线映射） */
var GIMBAL_PAN = 8, GIMBAL_TILT = 7;
var panAngle = 90, tiltAngle = 90;
function servoStep(n, delta){
  var v;
  if(n === GIMBAL_PAN){ panAngle = Math.max(15, Math.min(160, panAngle + delta)); v = panAngle; }
  else { tiltAngle = Math.max(15, Math.min(160, tiltAngle + delta)); v = tiltAngle; }
  api("/servo?n=" + n + "&a=" + v).then(setStatus);
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
  panAngle = 90; tiltAngle = 90;
  api("/servo?n=" + GIMBAL_PAN + "&a=90").then(function(){ return api("/servo?n=" + GIMBAL_TILT + "&a=90"); }).then(setStatus);
});

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
  videoBox.classList.add("show");
});
document.getElementById("videoBack").addEventListener("click", function(){
  videoImg.removeAttribute("src");
  videoBox.classList.remove("show");
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
