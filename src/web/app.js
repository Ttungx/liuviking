/* 小R小车控制台前端逻辑。
 *
 * 停车路径有三条，任何一条生效都能停：
 *   1) keyup / pointerup 主动发 /api/stop；
 *   2) 按住期间每 400ms 重发方向帧，页面被杀或断网后服务端 1.2s 看门狗自动 STOP；
 *   3) 切后台/失焦立即 STOP。
 * 另外用 stopSeq 代数补偿"STOP 之后才到达的在途 /cmd"。
 */
"use strict";

const MOTION = { w: "前进", s: "后退", a: "左转", d: "右转" };
const STATE_TEXT = {
  disconnected: "未连接",
  connecting: "连接中",
  connected: "已连接",
  error: "连接错误",
};
const REFRESH_MS = 400;
/* 弧线转向的内/外侧轮速，与车上部署版一致。
   2026-09-19 架空实测：内侧 30% @ 1kHz 硬 PWM 低于齿轮箱静摩擦死区，
   表现为"转向无力、只有外侧后轮在转、要先按一下前进才双侧转"。
   内侧抬到 58% 保证越过死区；落地后再按抓地力往 40 左右回调。 */
const ARC_INNER = 58;
const ARC_OUTER = 100;
/* 本浏览器会话 id：服务端用它做单控制端所有权。
   2026-09-19：改为 localStorage 持久化——切标签页/刷新后仍是同一个会话，
   不会被服务端 600s 会话看门狗当成"页面已死"踢下线（原先每次进页面随机生成）。 */
const SID = (() => {
  let sid = null;
  try { sid = localStorage.getItem("xr.sid"); } catch (e) {}
  if (!sid) {
    sid = Math.random().toString(36).slice(2) + Date.now().toString(36);
    try { localStorage.setItem("xr.sid", sid); } catch (e) {}
  }
  return sid;
})();
const $ = (id) => document.getElementById(id);
const api = (path) => {
  const url = path + (path.indexOf("?") < 0 ? "?" : "&") + "sid=" + SID;
  return fetch(url, { cache: "no-store" })
    .then((r) => r.json())
    .catch(() => null);
};

const setting = {
  read(key, fallback) {
    const value = localStorage.getItem("xr." + key);
    return value === null ? fallback : value;
  },
  save(key, value) {
    localStorage.setItem("xr." + key, value);
  },
};

let held = [];
let mouseKey = null;
let stopSeq = 0;
let refreshTimer = null;
let connState = "disconnected";
let locked = false;            // 别的会话持有控制权
let peerHost = null;           // 服务端已连接的实车 IP（浏览器直连视频用）
let arcMode = false;           // 默认原地转；弧线是实验选项，每次进页面显式切换（不持久化，防止残留旧状态）
let speedPreset = null;        // 已生效的轮速组合，避免每个刷新 tick 都重发
let prevPackets = 0;
let rate = null;
let logSeq = 0;

const activeKey = () => (held.length ? held[held.length - 1] : null);

// ---------------------------------------------------------------------------
// 运动
// ---------------------------------------------------------------------------

function applyArc(innerSide) {
  const outer = innerSide === "left" ? "right" : "left";
  return api("/api/speed?side=" + innerSide + "&value=" + ARC_INNER)
    .then(() => api("/api/speed?side=" + outer + "&value=" + ARC_OUTER));
}

function applyStraight() {
  return api("/api/speed?side=left&value=100").then(() => api("/api/speed?side=right&value=100"));
}

/* 弧线模式下 A/D 实际下发的是"前进 + 不对称轮速"，直行时先恢复对称速度 */
function sendPreset(key) {
  const wanted = arcMode && (key === "a" || key === "d") ? "arc-" + key : "straight";
  if (wanted === speedPreset) return Promise.resolve();
  speedPreset = wanted;
  if (wanted === "arc-a") return applyArc("left");
  if (wanted === "arc-d") return applyArc("right");
  return applyStraight();
}

function commandedKey(key) {
  return arcMode && (key === "a" || key === "d") ? "w" : key;
}

function sendKey(key) {
  const dispatched = stopSeq;
  sendPreset(key)
    .then(() => api("/api/cmd?k=" + commandedKey(key)))
    .then((data) => {
      if (!data) {
        stop("请求失败");
        return;
      }
      if (data.busy && !data.ok) {
        giveUpControl(data.reason);
        return;
      }
      if (!data.ok) {
        stop(data.reason || "命令失败");
        return;
      }
      applyStatus(data);
      // 这条 /cmd 出发后发生过 /stop（松手/急停/切后台），它可能比 STOP 晚到并
      // 重新锁存电机 —— 立刻补发一次停车。
      if (stopSeq !== dispatched) {
        api("/api/stop?why=" + encodeURIComponent("在途命令补偿"));
      }
    });
}

function press(key) {
  if (!MOTION[key] || locked) return;
  if (!held.includes(key)) held.push(key);
  paintActive();
  startRefresh();
  sendKey(key);
}

function release(key) {
  held = held.filter((k) => k !== key);
  paintActive();
  if (!held.length) stopRefresh();
  const current = activeKey();
  if (current) sendKey(current);
  else stop("松开 " + key.toUpperCase());
}

function stop(reason) {
  stopSeq++;
  held = [];
  mouseKey = null;
  speedPreset = null;   // 停车后轮速可能已被服务端复位，下次按方向键要重发
  stopRefresh();
  paintActive();
  api("/api/stop?why=" + encodeURIComponent(reason || "浏览器")).then(applyStatus);
}

/* 被别的会话持有：本地立即松手并亮出"取得控制"，但急停权利保留 */
function giveUpControl(reason) {
  stopSeq++;
  held = [];
  mouseKey = null;
  speedPreset = null;
  stopRefresh();
  paintActive();
  locked = true;
  paintLock(reason || "其它设备控制中");
}

function startRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(() => {
    const key = activeKey();
    if (key) sendKey(key);
    else stopRefresh();
  }, REFRESH_MS);
}

function stopRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

function paintActive() {
  const key = activeKey();
  document.querySelectorAll(".cap[data-k]").forEach((cap) => {
    cap.classList.toggle("active", cap.dataset.k === key);
  });
  const label = $("direction");
  label.textContent = key ? `${MOTION[key]} ${key.toUpperCase()}` : "停止";
  label.classList.toggle("active", Boolean(key));
}

function isTyping(target) {
  if (!target) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
}

window.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey || isTyping(event.target)) return;
  const key = (event.key || "").toLowerCase();
  if (key === " " || key === "spacebar") {
    event.preventDefault();
    stop("Space");
    return;
  }
  if (key === "escape") {
    event.preventDefault();
    stop("Esc");
    api("/api/disconnect").then(applyStatus);
    return;
  }
  const gimbalKey = GIMBAL_KEYS[event.key];
  if (gimbalKey) {
    // 不 preventDefault 的话浏览器会去滚动页面，而不是转云台
    event.preventDefault();
    pressGimbalKey(event.key);
    return;
  }
  if (MOTION[key]) {
    // 含 OS 按键重复：每次按下都算"键还活着"的刷新
    event.preventDefault();
    press(key);
  }
});

window.addEventListener("keyup", (event) => {
  const key = (event.key || "").toLowerCase();
  if (GIMBAL_KEYS[event.key]) {
    releaseGimbalKey(event.key);
    return;
  }
  if (MOTION[key]) {
    event.preventDefault();
    release(key);
  }
});

document.querySelectorAll(".cap[data-k]").forEach((cap) => {
  cap.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    mouseKey = cap.dataset.k;
    press(cap.dataset.k);
  });
});

const dropMouse = () => {
  if (mouseKey) {
    release(mouseKey);
    mouseKey = null;
  }
};
window.addEventListener("pointerup", dropMouse);
window.addEventListener("pointercancel", dropMouse);

$("stopBtn").addEventListener("click", () => stop("STOP 键"));
$("claimBtn").addEventListener("click", () => {
  speedPreset = null;
  api("/api/claim").then(applyStatus);
});
$("turnMode").addEventListener("click", () => {
  arcMode = !arcMode;
  speedPreset = null;      // 切换模式后下一条命令要重发轮速
  paintTurnMode();
});

function paintTurnMode() {
  $("turnMode").textContent = arcMode ? "弧线" : "原地";
  $("turnMode").classList.toggle("on", arcMode);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (!autoActive) stop("切后台");
  } else {
    // 2026-09-19：回到前台立刻续期，不等下一个被节流的轮询节拍，
    // 否则后台期间 setInterval 被浏览器压到 >=1s，会话看门狗可能误判页面已死
    api("/api/status").then(applyStatus);
  }
});
window.addEventListener("blur", () => { if (!autoActive) stop("窗口失焦"); });

// ---------------------------------------------------------------------------
// 状态与日志
// ---------------------------------------------------------------------------

function applyStatus(data) {
  if (!data) return;
  connState = data.state;
  if (data.state === "connected" && pendingTarget) {
    setting.save("host", pendingTarget.host);
    setting.save("port", pendingTarget.port);
    pendingTarget = null;
  }
  if (typeof data.busy === "boolean") locked = Boolean(data.busy) && !data.mine;

  $("dot").className = "dot " + (locked ? "connecting" : data.state);
  const base = STATE_TEXT[data.state] + (data.detail ? " · " + data.detail : "");
  $("statusText").textContent = locked ? "其它设备控制中 · " + base : base;
  $("claimBtn").hidden = !locked;
  $("connectBtn").textContent = data.state === "connected" ? "断开" : "连接";
  $("telemetry").textContent =
    rate === null ? `发送 ${data.packets} 帧` : `发送 ${data.packets} 帧 · ${rate} 帧/s`;
  $("frame").textContent = data.frame
    ? `${data.frame} · ${data.frame_age}s 前`
    : "等待指令";
  // 被锁定时只禁运动输入：急停、看日志、调速/云台这些不受影响的照常可用
  const live = data.state === "connected";
  paintPad();
  document.querySelector(".gimbal-pad").classList.toggle("off", !live);
  [
    $("applySpeed"), $("applyServo"), $("centerServo"), $("lightBtn"),
    $("panMinus"), $("panPlus"), $("tiltMinus"), $("tiltPlus"),
    $("gimbalHome"), $("setHome"),
  ].forEach((button) => {
    button.disabled = !live;
  });
  if (typeof data.peer === "string" && data.peer && data.peer !== peerHost) {
    peerHost = data.peer;
    if ($("videoOn").checked) applyVideo();  // 连上后立即用真实 IP 重载视频
  }
  applyGimbal(data);
}

function paintLock(text) {
  $("dot").className = "dot connecting";
  $("statusText").textContent = text;
  $("claimBtn").hidden = false;
  document.querySelector(".pad").classList.add("off");
}

function tick() {
  api("/api/status").then((data) => {
    if (!data) return;
    rate = (data.packets - prevPackets).toFixed(1);
    prevPackets = data.packets;
    applyStatus(data);
  });
}

function pullLogs() {
  api("/api/logs?since=" + logSeq).then((data) => {
    if (!data || !data.lines || !data.lines.length) return;
    logSeq = data.seq;
    const box = $("log");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    for (const line of data.lines) {
      const row = document.createElement("div");
      row.className = line.level;
      row.textContent = line.text;
      box.appendChild(row);
    }
    while (box.childElementCount > 600) box.removeChild(box.firstElementChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  });
}

$("clearLog").addEventListener("click", () => {
  $("log").textContent = "";
});

// ---------------------------------------------------------------------------
// 连接
// ---------------------------------------------------------------------------

function readTarget() {
  return { host: $("host").value.trim(), port: $("port").value.trim() || "2001" };
}

let pendingTarget = null;

$("connectBtn").addEventListener("click", () => {
  if (connState === "connected" || connState === "connecting") {
    api("/api/disconnect").then(applyStatus);
    return;
  }
  const target = readTarget();
  if (!target.host) {
    $("statusText").textContent = "请输入小车 IP";
    return;
  }
  // 只有连上了才落盘：失败过的地址不该被记住并盖掉后面的默认值
  pendingTarget = target;
  api(
    `/api/connect?host=${encodeURIComponent(target.host)}&port=${encodeURIComponent(target.port)}`
  ).then(applyStatus);
});

// ---------------------------------------------------------------------------
// 轮速 / 校准
// ---------------------------------------------------------------------------

function bindSlider(input) {
  const output = input.parentElement.querySelector("output");
  const unit = input.id === "servoAngle" ? "°" : "%";
  const sync = () => {
    output.textContent = input.value + unit;
  };
  input.addEventListener("input", sync);
  sync();
  return sync;
}

[$("leftSpeed"), $("rightSpeed"), $("servoAngle")].forEach(bindSlider);

$("applySpeed").addEventListener("click", () => {
  const left = $("leftSpeed").value;
  const right = $("rightSpeed").value;
  setting.save("leftSpeed", left);
  setting.save("rightSpeed", right);
  api(`/api/speed?side=left&value=${left}`)
    .then(() => api(`/api/speed?side=right&value=${right}`))
    .then(applyStatus);
});

// ---------------------------------------------------------------------------
// 云台 / 车灯
// ---------------------------------------------------------------------------

function sendServo(angle) {
  $("servoAngle").value = angle;
  $("servoAngle").dispatchEvent(new Event("input"));
  setting.save("servoNum", $("servoNum").value);
  api(`/api/servo?n=${$("servoNum").value}&a=${angle}`).then(applyStatus);
}

$("applyServo").addEventListener("click", () => sendServo($("servoAngle").value));
$("centerServo").addEventListener("click", () => sendServo(90));

// ---------------------------------------------------------------------------
// 云台：触控板/滑块无级调节 + 方向键连续小步
//
// 姿态、归位点、行程限位都由服务端权威记录，页面只跟随返回值（本地先行推进，
// 回显再校正）。通道映射为 2026-09-18 实车修正结论：水平 7 号、垂直 8 号，
// 水平 + 为左转、垂直 - 为上仰。
//
// 方向键不再依赖系统按键重复（那个节拍忽快忽慢），而是自己用 60ms 定时器
// 每 tick 走 2°，所以按住是平滑连续转动而不是 10° 一跳。
// ---------------------------------------------------------------------------

const GIMBAL_KEYS = {
  ArrowUp: ["tilt", -1],
  ArrowDown: ["tilt", 1],
  ArrowLeft: ["pan", 1],
  ArrowRight: ["pan", -1],
};
const GIMBAL_STEP = { pan: 2, tilt: 2 };
const GIMBAL_REPEAT_MS = 60;
const GIMBAL_SEND_MS = 90;

const gimbal = { pan: 90, tilt: 90 };
let gimbalHomePose = { pan: 90, tilt: 90 };
let gimbalLimits = { pan: [0, 185], tilt: [78, 170] };
const gimbalHeld = new Set();
let gimbalRepeat = null;
let gimbalPending = null;
let gimbalFlushTimer = null;
let dragging = null; // 'pan' | 'tilt' | 'pad'：拖动中的控件不被服务端回显抢走

function clampAxis(axis, value) {
  const [low, high] = gimbalLimits[axis];
  return Math.max(low, Math.min(high, Math.round(value)));
}

function sendGimbal(pan, tilt, now) {
  const patch = {};
  if (pan !== null && pan !== undefined) patch.pan = clampAxis("pan", pan);
  if (tilt !== null && tilt !== undefined) patch.tilt = clampAxis("tilt", tilt);
  if (!Object.keys(patch).length) return;
  Object.assign(gimbal, patch);
  paintGimbal();
  gimbalPending = Object.assign(gimbalPending || {}, patch);
  if (now) {
    flushGimbal();
    return;
  }
  if (!gimbalFlushTimer) gimbalFlushTimer = setTimeout(flushGimbal, GIMBAL_SEND_MS);
}

function flushGimbal() {
  if (gimbalFlushTimer) {
    clearTimeout(gimbalFlushTimer);
    gimbalFlushTimer = null;
  }
  if (!gimbalPending) return;
  const query = new URLSearchParams(gimbalPending).toString();
  gimbalPending = null;
  api("/api/gimbal?" + query).then(applyGimbal);
}

function applyGimbal(data) {
  if (!data || !data.gimbal) return;
  gimbal.pan = data.gimbal.pan;
  gimbal.tilt = data.gimbal.tilt;
  if (data.home) gimbalHomePose = data.home;
  if (data.gimbal_limits) {
    gimbalLimits = data.gimbal_limits;
    applyGimbalLimits();
  }
  paintGimbal();
}

function applyGimbalLimits() {
  $("panRange").min = gimbalLimits.pan[0];
  $("panRange").max = gimbalLimits.pan[1];
  $("tiltRange").min = gimbalLimits.tilt[0];
  $("tiltRange").max = gimbalLimits.tilt[1];
}

function showRange(id, value) {
  const input = $(id);
  input.value = value;
  input.nextElementSibling.textContent = value + "°";
}

function paintGimbal() {
  $("gimbalPos").textContent = `H ${gimbal.pan}° · V ${gimbal.tilt}°`;
  const [panLow, panHigh] = gimbalLimits.pan;
  const [tiltLow, tiltHigh] = gimbalLimits.tilt;
  // 端点姿态下把旋钮收在板内，否则 translate(-50%) 会让它探出边框
  const pct = (ratio) => Math.max(5, Math.min(95, ratio * 100));
  $("gimbalKnob").style.left = pct((panHigh - gimbal.pan) / (panHigh - panLow)) + "%";
  $("gimbalKnob").style.top = pct((gimbal.tilt - tiltLow) / (tiltHigh - tiltLow)) + "%";
  if (dragging !== "pan") showRange("panRange", gimbal.pan);
  if (dragging !== "tilt") showRange("tiltRange", gimbal.tilt);
}

// -- 方向键连续动作 --------------------------------------------------------

function gimbalTick() {
  if (!gimbalHeld.size) {
    stopGimbalRepeat();
    return;
  }
  let pan = null;
  let tilt = null;
  for (const key of gimbalHeld) {
    const [axis, dir] = GIMBAL_KEYS[key];
    const delta = GIMBAL_STEP[axis] * dir;
    if (axis === "pan") pan = (pan === null ? gimbal.pan : pan) + delta;
    else tilt = (tilt === null ? gimbal.tilt : tilt) + delta;
  }
  sendGimbal(pan, tilt, true);
}

function startGimbalRepeat() {
  if (!gimbalRepeat) gimbalRepeat = setInterval(gimbalTick, GIMBAL_REPEAT_MS);
}

function stopGimbalRepeat() {
  if (gimbalRepeat) {
    clearInterval(gimbalRepeat);
    gimbalRepeat = null;
  }
}

function pressGimbalKey(key) {
  gimbalHeld.add(key);
  startGimbalRepeat();
  gimbalTick();
}

function releaseGimbalKey(key) {
  gimbalHeld.delete(key);
  if (!gimbalHeld.size) stopGimbalRepeat();
}

// -- 触控板 ----------------------------------------------------------------

const gimbalPad = $("gimbalPad");
let padActive = false;

function padTarget(clientX, clientY) {
  const rect = gimbalPad.getBoundingClientRect();
  const x = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const y = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
  const [panLow, panHigh] = gimbalLimits.pan;
  const [tiltLow, tiltHigh] = gimbalLimits.tilt;
  return { pan: panHigh - x * (panHigh - panLow), tilt: tiltLow + y * (tiltHigh - tiltLow) };
}

function padMove(clientX, clientY) {
  const target = padTarget(clientX, clientY);
  sendGimbal(target.pan, target.tilt);
}

gimbalPad.addEventListener("pointerdown", (event) => {
  event.preventDefault();
  if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
  padActive = true;
  dragging = "pad";
  try {
    if (gimbalPad.setPointerCapture) gimbalPad.setPointerCapture(event.pointerId);
  } catch (error) {
    /* 指针已释放，忽略 */
  }
  padMove(event.clientX, event.clientY);
  flushGimbal();
});
gimbalPad.addEventListener("pointermove", (event) => {
  if (padActive) {
    event.preventDefault();
    padMove(event.clientX, event.clientY);
  }
});
const endPad = () => {
  if (!padActive) return;
  padActive = false;
  dragging = null;
  flushGimbal();
};
gimbalPad.addEventListener("pointerup", endPad);
gimbalPad.addEventListener("pointercancel", endPad);

// -- 滑块 ------------------------------------------------------------------

[["panRange", "pan"], ["tiltRange", "tilt"]].forEach(([id, axis]) => {
  const input = $(id);
  input.addEventListener("pointerdown", () => {
    dragging = axis;
  });
  input.addEventListener("input", () => {
    const value = Number(input.value);
    input.nextElementSibling.textContent = value + "°";
    sendGimbal(axis === "pan" ? value : null, axis === "tilt" ? value : null);
  });
  input.addEventListener("change", () => {
    dragging = null;
    flushGimbal();
  });
});

// -- 微调 / 归位 -----------------------------------------------------------

const fineStep = (axis, delta) => sendGimbal(axis === "pan" ? gimbal.pan + delta : null,
                                             axis === "tilt" ? gimbal.tilt + delta : null, true);
$("panMinus").addEventListener("click", () => fineStep("pan", -1));
$("panPlus").addEventListener("click", () => fineStep("pan", 1));
$("tiltMinus").addEventListener("click", () => fineStep("tilt", -1));
$("tiltPlus").addEventListener("click", () => fineStep("tilt", 1));

$("gimbalHome").addEventListener("click", () => {
  sendGimbal(gimbalHomePose.pan, gimbalHomePose.tilt, true);
});
$("setHome").addEventListener("click", () => {
  api("/api/gimbal_home").then(applyGimbal);
});

$("lightBtn").addEventListener("click", () => {
  const on = !$("lightBtn").classList.contains("on");
  api("/api/light?on=" + (on ? 1 : 0)).then(() => {
    $("lightBtn").classList.toggle("on", on);
    $("lightBtn").textContent = on ? "车灯：开" : "车灯：关";
  });
});

// ---------------------------------------------------------------------------
// 轨迹与路线：开环位置估计 + 车端脉冲反馈门控 + 路点跟随
//
// 坐标：x 向右、y 向上，θ 逆时针为正，0 = 朝 +x（地图右方）。
// 必须先标定：满速 = 100% 直行 3 秒的距离 ÷3（mm/s）；轮距 = 左右轮中心距。
// ---------------------------------------------------------------------------

const mapCanvas = $("mapCanvas");
const mapCtx = mapCanvas.getContext("2d");
let odom = { x: 0, y: 0, theta: 0, theta_deg: 0, path: [], route: null };
let autoActive = false;
let mapView = null;   // 画布取景 {cx, cy, scale}：稳定缩放，内容越界才重新取景
let mapViewLocked = false;  // 用户缩放或拖动画布后锁定取景，状态轮询不再抢回视图
let odomEnabled = setting.read("odomOn", "1") === "1";   // 开关有记忆：沿用上次状态
let routeOpenLoop = setting.read("routeOpenLoop", "1") === "1";  // 默认开放环：本车无编码器
let routeSemiAuto = setting.read("routeSemiAuto", "0") === "1";  // 半自动（"show"）：节点人工转向
let routePoints = [];       // 规划路线（画布上绘制，[x,y] mm）
let routeTouched = false;   // 用户改过路线后，不再从服务端旧路线恢复
let drawing = false;        // 左键拖拽绘制中
let mapPanning = false;     // Ctrl + 左键拖动画布
let mapPanStart = null;

const MAP_GRID_MM = 600;    // 一个网格单元 = 60cm
const MAP_MIN_SCALE = 0.03;
const MAP_MAX_SCALE = 4;

function readPositive(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

function readNumber(id, fallback) {
  const value = Number($(id).value);
  return Number.isFinite(value) ? value : fallback;
}

/* 屏幕坐标 -> 世界坐标（mm），与 drawMap 的取景保持一致 */
function canvasWorld(event) {
  const rect = mapCanvas.getBoundingClientRect();
  const view = mapView || { cx: 0, cy: 0, scale: 1 };
  return {
    x: view.cx + (event.clientX - rect.left - rect.width / 2) / view.scale,
    y: view.cy - (event.clientY - rect.top - rect.height / 2) / view.scale,
  };
}

/* 追加路线点：与上一点至少隔 50mm，最多 200 点（路线跟随容差 80mm，不必更密） */
function addRoutePoint(point) {
  const last = routePoints[routePoints.length - 1];
  if (last && Math.hypot(point.x - last[0], point.y - last[1]) < 50) return;
  if (routePoints.length >= 200) return;
  routePoints.push([Math.round(point.x), Math.round(point.y)]);
  routeTouched = true;
  drawMap();
}

mapCanvas.addEventListener("pointerdown", (event) => {
  if (!odomEnabled || event.button !== 0) return;
  event.preventDefault();
  if (event.ctrlKey) {
    if (!mapView) drawMap();
    mapViewLocked = true;
    mapPanning = true;
    mapPanStart = {
      clientX: event.clientX,
      clientY: event.clientY,
      cx: mapView.cx,
      cy: mapView.cy,
    };
    mapCanvas.classList.add("panning");
    try { mapCanvas.setPointerCapture(event.pointerId); } catch (error) { /* 已释放 */ }
    return;
  }
  if (autoActive) return;
  drawing = true;
  try { mapCanvas.setPointerCapture(event.pointerId); } catch (error) { /* 已释放 */ }
  addRoutePoint(canvasWorld(event));
});
mapCanvas.addEventListener("pointermove", (event) => {
  if (mapPanning && mapView && mapPanStart) {
    event.preventDefault();
    mapView.cx = mapPanStart.cx - (event.clientX - mapPanStart.clientX) / mapView.scale;
    mapView.cy = mapPanStart.cy + (event.clientY - mapPanStart.clientY) / mapView.scale;
    drawMap();
    return;
  }
  if (!drawing) return;
  event.preventDefault();
  addRoutePoint(canvasWorld(event));
});
mapCanvas.addEventListener("wheel", (event) => {
  if (!odomEnabled || !mapView) return;
  event.preventDefault();
  const rect = mapCanvas.getBoundingClientRect();
  const width = mapCanvas.clientWidth || 360;
  const height = mapCanvas.clientHeight || 240;
  const mouseX = event.clientX - rect.left - width / 2;
  const mouseY = event.clientY - rect.top - height / 2;
  const worldX = mapView.cx + mouseX / mapView.scale;
  const worldY = mapView.cy - mouseY / mapView.scale;
  const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
  const nextScale = Math.max(MAP_MIN_SCALE, Math.min(MAP_MAX_SCALE, mapView.scale * factor));
  mapView.scale = nextScale;
  mapView.cx = worldX - mouseX / nextScale;
  mapView.cy = worldY + mouseY / nextScale;
  mapViewLocked = true;
  drawMap();
}, { passive: false });
const endDraw = (event) => {
  if (mapPanning) {
    mapPanning = false;
    mapPanStart = null;
    mapCanvas.classList.remove("panning");
    try { mapCanvas.releasePointerCapture(event.pointerId); } catch (error) { /* 已释放 */ }
    return;
  }
  if (!drawing) return;
  drawing = false;
  try { mapCanvas.releasePointerCapture(event.pointerId); } catch (error) { /* 已释放 */ }
};
mapCanvas.addEventListener("pointerup", endDraw);
mapCanvas.addEventListener("pointercancel", endDraw);
mapCanvas.addEventListener("lostpointercapture", () => {
  drawing = false;
  mapPanning = false;
  mapPanStart = null;
  mapCanvas.classList.remove("panning");
});
/* 右键连点：每点一个路点，自动与上一点连线 */
mapCanvas.addEventListener("contextmenu", (event) => {
  if (!odomEnabled || autoActive) return;
  event.preventDefault();
  addRoutePoint(canvasWorld(event));
});

function odomParams() {
  return {
    track: readPositive("trackWidth", 120),
    vmax: readPositive("vmax", 250),
    left: readPositive("leftScale", 1),
    right: readPositive("rightScale", 1),
    dead: Math.max(0, readNumber("deadzone", 0)),
    tol: readPositive("routeTol", 12),
    heading: readNumber("routeHeading", 0),
  };
}

function paintPad() {
  const live = connState === "connected";
  document.querySelector(".pad").classList.toggle("off", !live || locked || autoActive);
}

function paintOdomEnabled() {
  $("odomOn").checked = odomEnabled;
  $("routeOpenLoop").checked = routeOpenLoop;
  $("routeSemiAuto").checked = routeSemiAuto;
  document.querySelector(".map").classList.toggle("off", !odomEnabled);
  ["trackWidth", "vmax", "leftScale", "rightScale", "deadzone", "routeTol", "routeHeading",
   "routeStart", "routeStop", "routeClear", "odomReset", "mapReset", "routeOpenLoop",
   "routeSemiAuto", "routeAlignResume"].forEach((id) => {
    $(id).disabled = !odomEnabled;
  });
  if (!odomEnabled) $("odomPose").textContent = "未启用";
}

function applyOdom(data) {
  if (!data || !odomEnabled) return;
  if (typeof data.x === "number") {
    odom = data;
    const feedback = data.route && data.route.motion_feedback;
    const feedbackText = data.route && data.route.active
      ? (data.route.require_feedback
          ? (feedback && feedback.available ? " · 车端反馈已接入" : " · 等待车端反馈")
          : " · 开放环执行（轨迹为估算）")
      : " · 开环估计";
    $("odomPose").textContent = `x ${data.x} · y ${data.y} · θ ${data.theta_deg}°${feedbackText}`;
  }
  if (data.route) {
    autoActive = Boolean(data.route.active);
    $("routeAlignResume").hidden = !data.route.waiting_manual_turn;
    // 路线启动后以服务端平滑后的路线为准，规划线和执行线必须是同一条几何路径。
    if (data.route.active && data.route.waypoints && data.route.waypoints.length) {
      routePoints = data.route.waypoints.map((pt) => [pt[0], pt[1]]);
    } else if (!routeTouched && !routePoints.length && data.route.waypoints && data.route.waypoints.length) {
      routePoints = data.route.waypoints.map((pt) => [pt[0], pt[1]]);
    }
    if (data.route.note) {
      const progress = data.route.total
        ? `（路点 ${Math.min(data.route.index + 1, data.route.total)}/${data.route.total}）`
        : "";
      $("routeNote").textContent = data.route.note + progress;
    }
  } else if (data.reason) {
    $("routeNote").textContent = data.reason;
  }
  paintPad();
  drawMap();
}

function viewCovers(view, minX, minY, maxX, maxY, width, height) {
  const halfW = (width / 2) / view.scale;
  const halfH = (height / 2) / view.scale;
  const padX = halfW * 0.1;
  const padY = halfH * 0.1;
  return (
    minX >= view.cx - halfW + padX && maxX <= view.cx + halfW - padX &&
    minY >= view.cy - halfH + padY && maxY <= view.cy + halfH - padY
  );
}

function drawMap() {
  const dpr = window.devicePixelRatio || 1;
  const width = mapCanvas.clientWidth || 360;
  const height = mapCanvas.clientHeight || 240;
  if (mapCanvas.width !== Math.round(width * dpr) || mapCanvas.height !== Math.round(height * dpr)) {
    mapCanvas.width = Math.round(width * dpr);
    mapCanvas.height = Math.round(height * dpr);
  }
  const ctx = mapCtx;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const points = [[0, 0], [odom.x, odom.y]];
  for (const pt of odom.path || []) points.push(pt);
  const waypoints = routePoints;
  for (const pt of waypoints) points.push(pt);
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const [x, y] of points) {
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  }
  const spanX = Math.max(maxX - minX, 500) * 1.25;
  const spanY = Math.max(maxY - minY, 350) * 1.25;
  if (!mapView || (!mapViewLocked && !viewCovers(mapView, minX, minY, maxX, maxY, width, height))) {
    mapView = {
      cx: (minX + maxX) / 2,
      cy: (minY + maxY) / 2,
      scale: Math.min(width / spanX, height / spanY),
    };
  }
  const cx = mapView.cx;
  const cy = mapView.cy;
  const scale = mapView.scale;
  const px = (x) => width / 2 + (x - cx) * scale;
  const py = (y) => height / 2 - (y - cy) * scale;

  // 600mm 网格：一个单元格边长 60cm
  ctx.strokeStyle = "rgba(148,163,184,0.14)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  const x0 = cx - width / (2 * scale), x1 = cx + width / (2 * scale);
  const y0 = cy - height / (2 * scale), y1 = cy + height / (2 * scale);
  for (let gx = Math.ceil(x0 / MAP_GRID_MM) * MAP_GRID_MM; gx <= x1; gx += MAP_GRID_MM) {
    ctx.moveTo(px(gx), 0); ctx.lineTo(px(gx), height);
  }
  for (let gy = Math.ceil(y0 / MAP_GRID_MM) * MAP_GRID_MM; gy <= y1; gy += MAP_GRID_MM) {
    ctx.moveTo(0, py(gy)); ctx.lineTo(width, py(gy));
  }
  ctx.stroke();

  // 原点坐标轴
  ctx.strokeStyle = "rgba(148,163,184,0.35)";
  ctx.beginPath();
  ctx.moveTo(px(0), 0); ctx.lineTo(px(0), height);
  ctx.moveTo(0, py(0)); ctx.lineTo(width, py(0));
  ctx.stroke();

  // 规划路线：从原点出发的橙色虚线 + 路点
  if (waypoints.length) {
    ctx.strokeStyle = "#f59e0b";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(px(0), py(0));
    for (const [x, y] of waypoints) ctx.lineTo(px(x), py(y));
    ctx.stroke();
    ctx.setLineDash([]);
    waypoints.forEach(([x, y], index) => {
      const active = odom.route && odom.route.active && index === odom.route.index;
      ctx.fillStyle = active ? "#fbbf24" : "#f59e0b";
      ctx.beginPath();
      ctx.arc(px(x), py(y), active ? 5 : 3.5, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  // 开环估计轨迹，不代表车体真实位置
  const path = odom.path || [];
  if (path.length > 1) {
    ctx.strokeStyle = "#60a5fa";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(px(path[0][0]), py(path[0][1]));
    for (const [x, y] of path) ctx.lineTo(px(x), py(y));
    ctx.stroke();
  }

  // 车：三角箭头朝 θ（0 = 地图右方）
  ctx.save();
  ctx.translate(px(odom.x), py(odom.y));
  ctx.rotate(-odom.theta);
  ctx.fillStyle = "#e2e8f0";
  ctx.beginPath();
  ctx.moveTo(10, 0);
  ctx.lineTo(-7, 6);
  ctx.lineTo(-7, -6);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function startRoute() {
  if (!odomEnabled) return;
  if (!routePoints.length) {
    $("routeNote").textContent = "请先在图上画路线：左键拖拽自由绘制 / 右键逐点连线";
    return;
  }
  const params = odomParams();
  const wp = encodeURIComponent(routePoints.map((item) => item.join(",")).join(";"));
  api(`/api/route?wp=${wp}&track=${params.track}&vmax=${params.vmax}` +
      `&left=${params.left}&right=${params.right}&dead=${params.dead}` +
      `&tol=${params.tol}&heading=${params.heading}&fb=${routeOpenLoop ? 0 : 1}` +
      `&semi=${routeSemiAuto ? 1 : 0}`)
    .then(applyOdom);
}

$("routeStart").addEventListener("click", startRoute);
$("routeStop").addEventListener("click", () => api("/api/route_stop").then(applyOdom));
$("routeClear").addEventListener("click", () => {
  if (!odomEnabled) return;
  routePoints = [];
  routeTouched = true;
  $("routeNote").textContent = "路线已清空：左键拖拽自由绘制 / 右键逐点连线";
  if (autoActive) {          // 正在行驶时清空 = 停止路线
    autoActive = false;
    paintPad();
    api("/api/route_stop");
  }
  drawMap();
});
$("mapReset").addEventListener("click", () => {
  if (!odomEnabled) return;
  mapView = null;
  mapViewLocked = false;
  drawMap();
});
$("odomOn").addEventListener("change", () => {
  odomEnabled = $("odomOn").checked;
  setting.save("odomOn", odomEnabled ? "1" : "0");   // 记忆，下次打开控制台沿用
  paintOdomEnabled();
  if (odomEnabled) {
    api("/api/odom").then(applyOdom);
  } else if (autoActive) {
    autoActive = false;      // 关闭模块时一并停止正在行驶的路线
    paintPad();
    api("/api/route_stop");
  }
});
$("routeOpenLoop").addEventListener("change", () => {
  routeOpenLoop = $("routeOpenLoop").checked;
  setting.save("routeOpenLoop", routeOpenLoop ? "1" : "0");   // 记忆，下次沿用
});
$("routeSemiAuto").addEventListener("change", () => {
  routeSemiAuto = $("routeSemiAuto").checked;
  setting.save("routeSemiAuto", routeSemiAuto ? "1" : "0");   // 记忆，下次沿用
});
$("routeAlignResume").addEventListener("click", () => api("/api/route_align").then(applyOdom));
$("odomReset").addEventListener("click", () => {
  if (!odomEnabled) return;
  const params = odomParams();
  api(`/api/odom_cfg?track=${params.track}&vmax=${params.vmax}` +
      `&left=${params.left}&right=${params.right}&dead=${params.dead}`)
    .then(() => api("/api/odom_reset?x=0&y=0&theta_deg=0"))
    .then(applyOdom);
});
["trackWidth", "vmax", "leftScale", "rightScale", "deadzone", "routeTol", "routeHeading"].forEach((id) =>
  $(id).addEventListener("change", () => setting.save(id, $(id).value))
);
setInterval(() => {
  if (odomEnabled) api("/api/odom").then(applyOdom);
}, 100);

// ---------------------------------------------------------------------------
// 视频：浏览器直连小车的 MJPEG 端口，不经过本服务
// ---------------------------------------------------------------------------

const videoImg = $("videoImg");
const videoHint = $("videoHint");

function applyVideo() {
  // 优先用服务端已连上的实车 IP：系统代理解析不了 car.local 时会 502
  const host = peerHost || $("host").value.trim();
  const port = $("videoPort").value.trim() || "8080";
  const path = $("videoPath").value.trim() || "/?action=stream";
  setting.save("videoPort", port);
  setting.save("videoPath", path);
  if (!$("videoOn").checked || !host) {
    videoImg.removeAttribute("src");
    videoImg.classList.remove("show");
    videoHint.style.display = "";
    videoHint.textContent = "视频未启用";
    return;
  }
  videoHint.style.display = "";
  videoHint.textContent = "正在连接视频…";
  videoImg.src = `http://${host}:${port}${path}`;
}

[$("videoOn"), $("videoPort"), $("videoPath")].forEach((input) =>
  input.addEventListener("change", applyVideo)
);
videoImg.addEventListener("error", () => {
  videoImg.classList.remove("show");
  videoHint.style.display = "";
  videoHint.textContent = "视频不可用（摄像头未接或地址不对）";
});
/* MJPEG 是无限流，部分浏览器不触发 load，用 naturalWidth 判定首帧已出图 */
setInterval(() => {
  if ($("videoOn").checked && videoImg.naturalWidth > 0) {
    videoImg.classList.add("show");
    videoHint.style.display = "none";
  }
}, 1000);

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

function restoreSettings() {
  $("host").value = setting.read("host", "car.local");
  $("port").value = setting.read("port", "2001");
  $("videoPort").value = setting.read("videoPort", "8080");
  $("videoPath").value = setting.read("videoPath", "/?action=stream");
  $("leftSpeed").value = setting.read("leftSpeed", "100");
  $("rightSpeed").value = setting.read("rightSpeed", "100");
  $("servoNum").value = setting.read("servoNum", "1");
  $("trackWidth").value = setting.read("trackWidth", "120");
  $("vmax").value = setting.read("vmax", "250");
  $("leftScale").value = setting.read("leftScale", "1");
  $("rightScale").value = setting.read("rightScale", "1");
  $("deadzone").value = setting.read("deadzone", "0");
  // 80mm 是旧版路点追踪的默认值，会在拐角前明显切弯；迁移旧默认值。
  const savedRouteTol = setting.read("routeTol", "");
  $("routeTol").value = savedRouteTol === "80" ? "12" : (savedRouteTol || "12");
  $("routeHeading").value = setting.read("routeHeading", "0");
  // 云台姿态不存本地：以服务端记录为准，连上后由 /api/status 回显
  [$("leftSpeed"), $("rightSpeed"), $("servoAngle")].forEach((input) =>
    input.dispatchEvent(new Event("input"))
  );
}

restoreSettings();
paintActive();
paintTurnMode();
paintOdomEnabled();
drawMap();
/* 打开页面即取控制权（与车上原部署版一致）；对方正按住方向键时会失败，
   随后状态里 busy=true，顶栏出现"取得控制"按钮 */
api("/api/claim").then((data) => applyStatus(data));
setInterval(tick, 1000);
setInterval(pullLogs, 1000);
