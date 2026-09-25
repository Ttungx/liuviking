"""PC 端网页控制台（src/web_console.py）的端到端测试。

用本机假机器人服务器接住真实字节流，核对 HTTP -> 协议帧 -> 停车/看门狗/空闲释放
的行为；不依赖浏览器。
"""

from __future__ import annotations

import json
import logging
import math
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402
from src import web_console  # noqa: E402
from src.web_console import Console, build_server  # noqa: E402

STOP = protocol.STOP
FORWARD = protocol.FORWARD
BACKWARD = protocol.BACKWARD
LEFT = protocol.LEFT


class RoutePhotoPlanTests(unittest.TestCase):
    def test_long_segment_photos_start_every_300mm_and_include_endpoint(self):
        schedule = Console._build_photo_schedule([(1800.0, 0.0)])
        self.assertEqual(schedule, {0: [0.0, 300.0, 600.0, 900.0, 1200.0, 1500.0, 1800.0]})

    def test_short_segment_has_no_photo_schedule(self):
        self.assertEqual(Console._build_photo_schedule([(1500.0, 0.0)]), {})

    def test_photo_schedule_consolidates_node_point_to_next_segment(self):
        """节点照片只留一份：本段终点交给下一段的 0mm 点（先对齐再拍）。"""
        schedule = Console._build_photo_schedule([(1600.0, 0.0), (1600.0, 1600.0)])
        self.assertEqual(schedule[0], [0.0, 300.0, 600.0, 900.0, 1200.0, 1500.0])
        self.assertEqual(schedule[1], [0.0, 300.0, 600.0, 900.0, 1200.0, 1500.0, 1600.0])

    def test_photo_schedule_keeps_segment_end_when_next_is_short(self):
        """下一段太短没有拍照计划时，本段终点照保留，节点不丢拍。"""
        schedule = Console._build_photo_schedule([(1600.0, 0.0), (1600.0, 600.0)])
        self.assertEqual(schedule[0], [0.0, 300.0, 600.0, 900.0, 1200.0, 1500.0, 1600.0])
        self.assertNotIn(1, schedule)

    def test_photo_waits_until_heading_aligned_with_segment(self):
        """拍照点先对齐：车头与所在段方向夹角超阈值时不拍，对齐后立即拍。"""
        console = Console()
        try:
            with console._lock:
                console._photo_schedule = {0: [0.0]}
                console._photo_cursor = {0: 0}
                console.odom.reset(theta=math.radians(90.0))  # 车头朝 +y，未对齐 +x 段
                self.assertIsNone(console._photo_due_locked(0, (0.0, 0.0), (1000.0, 0.0)))
                console.odom.reset(theta=math.radians(3.0))   # 对齐在容差内
                self.assertEqual(
                    console._photo_due_locked(0, (0.0, 0.0), (1000.0, 0.0)), 0.0)
        finally:
            console.close()

    def test_photo_pause_invalidates_previous_drive_command(self):
        console = Console()
        temp_dir = tempfile.TemporaryDirectory()
        original_fetch = web_console.fetch_snapshot
        original_settle = web_console.PHOTO_SETTLE_SECONDS
        try:
            web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
            web_console.PHOTO_SETTLE_SECONDS = 0.0
            console.client._host = "127.0.0.1"
            console._photo_route_dir = Path(temp_dir.name)
            console._route_last_drive = ("w", 100, 100)
            with console._lock:
                console._capture_photo_sequence_locked(0, 300.0)
            self.assertIsNone(console._route_last_drive)
        finally:
            web_console.fetch_snapshot = original_fetch
            web_console.PHOTO_SETTLE_SECONDS = original_settle
            console.close()
            temp_dir.cleanup()

    def test_photo_pause_reanchors_odom_clock(self):
        """拍照暂停握锁 ~2s，开始时要重锚里程计时钟：放开后不补积停车时间。"""
        console = Console()
        temp_dir = tempfile.TemporaryDirectory()
        original_fetch = web_console.fetch_snapshot
        original_settle = web_console.PHOTO_SETTLE_SECONDS
        try:
            web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
            web_console.PHOTO_SETTLE_SECONDS = 0.0
            console.client._host = "127.0.0.1"
            console._photo_route_dir = Path(temp_dir.name)
            with console._lock:
                console._last_odom_t = time.monotonic() - 2.0  # 模拟长时间未积分
                console._capture_photo_sequence_locked(0, 0.0)
                self.assertIsNone(console._last_odom_t)
        finally:
            web_console.fetch_snapshot = original_fetch
            web_console.PHOTO_SETTLE_SECONDS = original_settle
            console.close()
            temp_dir.cleanup()

    def test_photo_order_alternates_between_route_points(self):
        console = Console()
        temp_dir = tempfile.TemporaryDirectory()
        original_fetch = web_console.fetch_snapshot
        original_settle = web_console.PHOTO_SETTLE_SECONDS
        try:
            web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
            web_console.PHOTO_SETTLE_SECONDS = 0.0
            console.client._host = "127.0.0.1"
            console._photo_route_dir = Path(temp_dir.name)
            with console._lock:
                console._capture_photo_sequence_locked(0, 0.0)
                console._capture_photo_sequence_locked(0, 300.0)
                console._capture_photo_sequence_locked(0, 600.0)
            names = [item["file"].split("_")[-1] for item in console._photo_manifest]
            self.assertEqual(
                names,
                ["level.jpg", "middle.jpg", "peak.jpg",
                 "peak.jpg", "middle.jpg", "level.jpg",
                 "level.jpg", "middle.jpg", "peak.jpg"],
            )
        finally:
            web_console.fetch_snapshot = original_fetch
            web_console.PHOTO_SETTLE_SECONDS = original_settle
            console.close()
            temp_dir.cleanup()


class FakeRobotServer:
    """记录收到字节的单客户端 TCP 服务器。"""

    def __init__(self) -> None:
        self.received = bytearray()
        self.lock = threading.Lock()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                while True:
                    try:
                        data = self.request.recv(1024)
                    except OSError:
                        break
                    if not data:
                        break
                    with outer.lock:
                        outer.received.extend(data)

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def frames(self):
        with self.lock:
            return protocol.parse_stream(bytes(self.received))

    def wait_frames(self, count: int, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.frames()
            if len(frames) >= count:
                return frames
            time.sleep(0.02)
        return self.frames()


class StartupGuardTests(unittest.TestCase):
    def test_port_in_use_detects_existing_listener(self):
        """防双实例：端口已被监听时为真，释放后为假。"""
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        probe.listen(1)
        port = probe.getsockname()[1]
        try:
            self.assertTrue(web_console._port_in_use("127.0.0.1", port))
        finally:
            probe.close()
        self.assertFalse(web_console._port_in_use("127.0.0.1", port))


class ConsoleHttpTest(unittest.TestCase):
    MOTION_TIMEOUT = 0.4
    IDLE_TIMEOUT = 1.5

    def setUp(self) -> None:
        self.robot = FakeRobotServer()
        self.robot.start()
        self.console = Console(
            motion_timeout=self.MOTION_TIMEOUT,
            session_timeout=self.IDLE_TIMEOUT,
            require_motion_feedback=False,
        )
        self.server = build_server(self.console, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._http = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._http.start()
        self.handler = self.console.logs
        self._root_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger().addHandler(self.handler)

    def tearDown(self) -> None:
        logging.getLogger().removeHandler(self.handler)
        logging.getLogger().setLevel(self._root_level)
        self.console.close()
        self.server.shutdown()
        self.server.server_close()
        self.robot.stop()

    # -- 辅助 ------------------------------------------------------------

    def get(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def raw(self, path: str):
        with urllib.request.urlopen(self.base + path, timeout=3) as response:
            return response.status, response.read()

    def connect(self) -> dict:
        status = self.get("/api/connect?host=127.0.0.1&port=%d" % self.robot.port)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = self.get("/api/status")
            if status["state"] == "connected":
                return status
            time.sleep(0.02)
        self.fail("未能连上假机器人：%s" % status)

    # -- 用例 ------------------------------------------------------------

    def test_index_page_is_served(self) -> None:
        status, body = self.raw("/")
        self.assertEqual(status, 200)
        self.assertIn("小R小车控制台", body.decode("utf-8"))

    def test_static_assets_are_served(self) -> None:
        for path in ("/app.js", "/style.css"):
            status, body = self.raw(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(body, path)

    def test_unknown_path_is_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.raw("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_static_route_cannot_escape_web_dir(self) -> None:
        """只服务白名单三个文件：带路径分隔符的请求必须 404。"""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.raw("/../requirements.txt")
        self.assertEqual(ctx.exception.code, 404)

    def test_connect_sends_stop_first(self) -> None:
        status = self.connect()
        self.assertTrue(status["ok"])
        self.assertEqual(self.robot.wait_frames(1)[0], STOP)

    def test_local_host_falls_back_to_cached_ip(self) -> None:
        # Windows 上 .local 的 mDNS 解析时灵时不灵：解析失败必须用缓存 IP 兜底
        self.console.set_host_cache({"car.local": "127.0.0.1"})
        original = web_console._resolve_ipv4
        web_console._resolve_ipv4 = lambda host, port: None
        try:
            self.get("/api/connect?host=car.local&port=%d" % self.robot.port)
            deadline = time.monotonic() + 3.0
            state = ""
            while time.monotonic() < deadline:
                state = self.get("/api/status")["state"]
                if state == "connected":
                    break
                time.sleep(0.02)
        finally:
            web_console._resolve_ipv4 = original
        self.assertEqual(state, "connected")

    def test_odom_pose_and_config(self) -> None:
        self.connect()
        result = self.get("/api/odom_cfg?track=150&vmax=300&left=0.8&right=1.1&dead=12")
        self.assertTrue(result["ok"])
        self.assertEqual(result["cfg"]["track_mm"], 150.0)
        self.assertEqual(result["cfg"]["max_speed_mm_per_s"], 300.0)
        self.assertEqual(result["cfg"]["left_scale"], 0.8)
        self.assertEqual(result["cfg"]["right_scale"], 1.1)
        self.assertEqual(result["cfg"]["left_deadzone"], 12.0)
        self.assertEqual(result["cfg"]["right_deadzone"], 12.0)
        result = self.get("/api/odom")
        self.assertEqual(result["path"][0], [0.0, 0.0])
        self.assertFalse(result["route"]["active"])

    def test_route_accepts_initial_heading(self) -> None:
        self.connect()
        result = self.get("/api/route?wp=0,0&tol=20&heading=90")
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(result["theta_deg"], 90.0)

    def test_odometry_accumulates_while_driving(self) -> None:
        self.connect()
        # 模拟页面按键连发（持续续看门狗）：扣除 0.25s 起步延迟后仍应有位移
        deadline = time.monotonic() + 0.9
        while time.monotonic() < deadline:
            self.get("/api/cmd?k=w")
            time.sleep(0.15)
        self.get("/api/stop")
        pose = self.get("/api/odom")
        self.assertGreater(pose["x"], 20.0)
        self.assertLess(pose["x"], 200.0)

    def test_route_requires_connection(self) -> None:
        result = self.get("/api/route?wp=100,0")
        self.assertFalse(result["ok"])
        self.assertIn("未连接", result["reason"])

    def test_route_rejects_bad_waypoints(self) -> None:
        self.connect()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/route?wp=abc")
        self.assertEqual(ctx.exception.code, 400)

    def test_route_completes_trivial_waypoint(self) -> None:
        # 路点在容差内 -> 路线线程应立即判定到达并结束
        self.connect()
        result = self.get("/api/route?wp=50,0&tol=80&track=120&vmax=250")
        self.assertTrue(result["ok"])
        self.assertTrue(result["route"]["active"])
        deadline = time.monotonic() + 3.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertEqual(route["note"], "完成")

    def test_route_resumes_motion_after_photo_pause(self) -> None:
        self.connect()
        original_fetch = web_console.fetch_snapshot
        original_settle = web_console.PHOTO_SETTLE_SECONDS
        original_data_dir = web_console.PHOTO_DATA_DIR
        temp_dir = tempfile.TemporaryDirectory()
        try:
            web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
            web_console.PHOTO_SETTLE_SECONDS = 0.0
            web_console.PHOTO_DATA_DIR = Path(temp_dir.name)
            self.get("/api/route?wp=1800,0&vmax=2000&tol=12")
            deadline = time.monotonic() + 3.0
            resumed = False
            while time.monotonic() < deadline:
                frames = self.robot.frames()
                for index, frame in enumerate(frames[:-1]):
                    if frame == STOP and FORWARD in frames[index + 1:]:
                        resumed = True
                        break
                if resumed:
                    break
                time.sleep(0.02)
            self.assertTrue(resumed, "拍照 STOP 后没有恢复前进帧")
        finally:
            web_console.fetch_snapshot = original_fetch
            web_console.PHOTO_SETTLE_SECONDS = original_settle
            web_console.PHOTO_DATA_DIR = original_data_dir
            temp_dir.cleanup()

    def test_route_stops_without_motion_feedback(self) -> None:
        self.connect()
        self.console._require_motion_feedback = True
        result = self.get("/api/route?wp=1000,0&vmax=250&tol=12")
        self.assertTrue(result["ok"])
        deadline = time.monotonic() + 3.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertIn("无实际运动反馈", route["note"])

    def test_route_does_not_advance_photos_without_motion_evidence(self) -> None:
        """脉冲始终不变时只拍起点组：后续拍照点不得按开环估算推进。

        2026-09-25 真机：开环估算一到 300mm 就抢在 1s 反馈超窗前触发拍照。"""
        self.connect()
        self.console._require_motion_feedback = True
        sample = {"available": True, "pulse1": 7, "pulse2": 7, "voltage": 12}
        fetch_feedback = web_console._fetch_motion_feedback
        web_console._fetch_motion_feedback = lambda host: dict(sample)
        fetch_snapshot = web_console.fetch_snapshot
        web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
        settle = web_console.PHOTO_SETTLE_SECONDS
        web_console.PHOTO_SETTLE_SECONDS = 0.0
        data_dir = web_console.PHOTO_DATA_DIR
        temp_dir = tempfile.TemporaryDirectory()
        web_console.PHOTO_DATA_DIR = Path(temp_dir.name)
        self.addCleanup(setattr, web_console, "_fetch_motion_feedback", fetch_feedback)
        self.addCleanup(setattr, web_console, "fetch_snapshot", fetch_snapshot)
        self.addCleanup(setattr, web_console, "PHOTO_SETTLE_SECONDS", settle)
        self.addCleanup(setattr, web_console, "PHOTO_DATA_DIR", data_dir)
        self.addCleanup(temp_dir.cleanup)
        result = self.get("/api/route?wp=1800,0&vmax=800&tol=12")
        self.assertTrue(result["ok"])
        deadline = time.monotonic() + 6.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertIn("无实际运动反馈", route["note"])
        self.assertEqual(route["photos"]["done"], 1)  # 只有起点组

    def test_route_open_loop_continues_without_feedback(self) -> None:
        """fb=0（开放环）：没有脉冲反馈也把路线走完，拍照点按估算推进。

        2026-09-25：本车无编码器（计数恒 0），开放环是可用模式；严格门控
        默认仍在（不带 fb 参数时要求反馈），由调用方显式放开。"""
        self.connect()
        self.console._require_motion_feedback = True
        sample = {"available": True, "pulse1": 7, "pulse2": 7, "voltage": 12}
        fetch_feedback = web_console._fetch_motion_feedback
        web_console._fetch_motion_feedback = lambda host: dict(sample)
        fetch_snapshot = web_console.fetch_snapshot
        web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
        settle = web_console.PHOTO_SETTLE_SECONDS
        web_console.PHOTO_SETTLE_SECONDS = 0.0
        data_dir = web_console.PHOTO_DATA_DIR
        temp_dir = tempfile.TemporaryDirectory()
        web_console.PHOTO_DATA_DIR = Path(temp_dir.name)
        self.addCleanup(setattr, web_console, "_fetch_motion_feedback", fetch_feedback)
        self.addCleanup(setattr, web_console, "fetch_snapshot", fetch_snapshot)
        self.addCleanup(setattr, web_console, "PHOTO_SETTLE_SECONDS", settle)
        self.addCleanup(setattr, web_console, "PHOTO_DATA_DIR", data_dir)
        self.addCleanup(temp_dir.cleanup)
        result = self.get("/api/route?wp=1800,0&vmax=400&tol=40&fb=0")
        self.assertTrue(result["ok"])
        self.assertFalse(result["route"]["require_feedback"])
        deadline = time.monotonic() + 10.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertEqual(route["note"], "完成")
        self.assertEqual(route["photos"]["done"], 7)  # 开放环下拍照点按估算推进

    def test_semi_auto_route_waits_then_aligns_and_resumes(self) -> None:
        """半自动（semi=1）：节点拍完照停下等待，/api/route_align 对准下一段后继续。"""
        self.connect()
        self.console._require_motion_feedback = True
        sample = {"available": True, "pulse1": 7, "pulse2": 7, "voltage": 12}
        fetch_feedback = web_console._fetch_motion_feedback
        web_console._fetch_motion_feedback = lambda host: dict(sample)
        fetch_snapshot = web_console.fetch_snapshot
        web_console.fetch_snapshot = lambda _url: b"\xff\xd8fake-jpeg"
        settle = web_console.PHOTO_SETTLE_SECONDS
        web_console.PHOTO_SETTLE_SECONDS = 0.0
        data_dir = web_console.PHOTO_DATA_DIR
        temp_dir = tempfile.TemporaryDirectory()
        web_console.PHOTO_DATA_DIR = Path(temp_dir.name)
        self.addCleanup(setattr, web_console, "_fetch_motion_feedback", fetch_feedback)
        self.addCleanup(setattr, web_console, "fetch_snapshot", fetch_snapshot)
        self.addCleanup(setattr, web_console, "PHOTO_SETTLE_SECONDS", settle)
        self.addCleanup(setattr, web_console, "PHOTO_DATA_DIR", data_dir)
        self.addCleanup(temp_dir.cleanup)
        result = self.get("/api/route?wp=1600,0;1600,1600&vmax=400&tol=40&fb=0&semi=1")
        self.assertTrue(result["ok"])
        self.assertTrue(result["route"]["semi_auto"])
        deadline = time.monotonic() + 10.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if route.get("waiting_manual_turn"):
                break
            self.assertTrue(route["active"], "路线提前结束：%s" % route["note"])
            time.sleep(0.05)
        self.assertTrue(route.get("waiting_manual_turn"), "未进入等待人工转向")
        self.assertIn("人工转向", route["note"])
        # 等待期间：手动指令可用；松开按键的 STOP 只停车、不中止路线
        self.assertTrue(self.get("/api/cmd?k=w")["ok"])
        self.get("/api/stop?why=test")
        self.assertTrue(self.get("/api/odom")["route"]["active"])
        # 人工转向完成：对准下一段（+y），位置归零到节点
        aligned = self.get("/api/route_align")
        self.assertFalse(aligned["route"]["waiting_manual_turn"])
        self.assertAlmostEqual(aligned["theta_deg"], 90.0, places=1)
        self.assertAlmostEqual(aligned["x"], 1600.0, delta=1.0)
        self.assertAlmostEqual(aligned["y"], 0.0, delta=1.0)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertEqual(route["note"], "完成")

    def test_odom_does_not_backfill_after_long_lock(self) -> None:
        """里程计线程被长临界区憋住后，放开时不能拿陈旧 dt 补积一大段假位移。

        2026-09-25 真机：拍照暂停握锁 ~2s，放开后一次补积 ~500mm，拍照点被
        假位移提前触发、反馈超窗被连续打断。"""
        self.connect()
        self.console.odom.configure(max_speed_mm_per_s=250.0)
        with self.console._lock:
            self.console.odom.reset()
            self.console._duty["left"] = 100
            self.console._duty["right"] = 100
            self.console._last_cmd = "w"
            self.console._odom_key = "w"          # 已在行驶：本用例只测积分的 dt 封顶
            self.console._odom_lag_until = 0.0
            self.console._last_odom_t = time.monotonic() - 2.0  # 模拟被锁 2 秒
            self.console._odom_tick()
            x = self.console.odom.x
        self.assertLessEqual(x, web_console.ODOM_MAX_STEP_SECONDS * 250.0 + 1.0)

    def test_odom_waits_start_lag_after_command_start(self) -> None:
        """起步静摩擦延迟：按下的瞬间不产生位移估计，延迟过后才积分。"""
        self.connect()
        self.console.odom.configure(max_speed_mm_per_s=250.0)
        with self.console._lock:
            self.console.odom.reset()
            self.console._duty["left"] = 100
            self.console._duty["right"] = 100
            self.console._last_cmd = "w"
            self.console._odom_key = None          # 从静止起步
            self.console._odom_lag_until = 0.0
            self.console._last_odom_t = time.monotonic() - 0.5
            self.console._odom_tick()
            self.assertEqual(self.console.odom.x, 0.0)   # 延迟窗口内不动
            self.console._odom_lag_until = 0.0           # 越过延迟
            self.console._last_odom_t = time.monotonic() - 0.5
            self.console._odom_tick()
            self.assertGreater(self.console.odom.x, 0.0)

    def test_route_stop_keeps_reason_not_completed(self) -> None:
        """用户停止路线后 route.note 必须是停止原因，不能被收尾覆盖成"完成"。"""
        self.connect()
        result = self.get("/api/route?wp=1200,0&vmax=600&tol=12&fb=0")
        self.assertTrue(result["ok"])
        time.sleep(0.3)
        self.get("/api/route_stop")
        deadline = time.monotonic() + 3.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertEqual(route["note"], "用户停止路线")

    def test_route_stops_when_pulses_frozen_despite_drive_resends(self) -> None:
        """驱动帧按转向修正反复重发时，1s 脉冲超窗不能被重置而永不触发。

        2026-09-25 真机坑：直行时开环航向漂移让 polyline 每 50-150ms 变一次，
        重发把反馈窗口重置成“永远刚开始”，路线照跑到下一个拍照点/终点。"""
        self.connect()
        self.console._require_motion_feedback = True
        sample = {"available": True, "pulse1": 7, "pulse2": 7, "voltage": 12}
        fetch_orig = web_console._fetch_motion_feedback
        web_console._fetch_motion_feedback = lambda host: dict(sample)
        cmd_orig = web_console.polyline_command
        tick = {"n": 0}

        def jittery_command(*args, **kwargs):
            tick["n"] += 1
            cmd = cmd_orig(*args, **kwargs)
            cmd["left"] = 90 + (tick["n"] % 5)  # 每次调用都不同 -> 每 tick 重发
            return cmd

        web_console.polyline_command = jittery_command
        self.addCleanup(setattr, web_console, "_fetch_motion_feedback", fetch_orig)
        self.addCleanup(setattr, web_console, "polyline_command", cmd_orig)
        result = self.get("/api/route?wp=900,0&vmax=250&tol=12")
        self.assertTrue(result["ok"])
        deadline = time.monotonic() + 5.0
        route = result["route"]
        while time.monotonic() < deadline:
            route = self.get("/api/odom")["route"]
            if not route["active"]:
                break
            time.sleep(0.05)
        self.assertFalse(route["active"])
        self.assertIn("脉冲计数未变化", route["note"])

    def test_command_sends_motion_frame(self) -> None:
        self.connect()
        result = self.get("/api/cmd?k=w")
        self.assertTrue(result["ok"])
        self.assertEqual(result["frame"], protocol.packet_hex(FORWARD))
        self.assertTrue(result["motion_armed"])
        frames = self.robot.wait_frames(2)
        self.assertEqual(frames[1], FORWARD)

    def test_motion_command_rejected_while_disconnected(self) -> None:
        result = self.get("/api/cmd?k=w")
        self.assertFalse(result["ok"])
        self.assertIn("未连接", result["reason"])
        self.assertEqual(self.robot.frames(), [])

    def test_invalid_key_is_400(self) -> None:
        self.connect()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/cmd?k=x")
        self.assertEqual(ctx.exception.code, 400)

    def test_stop_sends_stop_frame_and_disarms(self) -> None:
        self.connect()
        self.get("/api/cmd?k=s")
        result = self.get("/api/stop?why=test")
        self.assertEqual(result["last"], None)
        self.assertFalse(result["motion_armed"])
        self.assertEqual(result["frame"], protocol.packet_hex(STOP))
        self.assertEqual(self.robot.wait_frames(3)[-1], STOP)

    def test_motion_watchdog_stops_without_refresh(self) -> None:
        self.connect()
        self.get("/api/cmd?k=w")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self.get("/api/status")["motion_armed"]:
                break
            time.sleep(0.05)
        status = self.get("/api/status")
        self.assertFalse(status["motion_armed"])
        self.assertEqual(status["frame"], protocol.packet_hex(STOP))
        # 2026-09-19：看门狗只 STOP，不再立刻把轮速复位 100/100（避免把
        # 弧线内侧轮拽回静摩擦死区）；对称速度改为下次直行前由前端重发。
        # 所以线上的最后一帧就是 STOP，而不是 STOP + 左100 + 右100。
        self.assertEqual(self.robot.frames()[-1], STOP)

    def test_status_polling_keeps_connection_alive(self) -> None:
        """回归：只轮询 /api/status 的浏览器不该被空闲超时踢下线。"""
        self.connect()
        deadline = time.monotonic() + self.IDLE_TIMEOUT * 2.5
        while time.monotonic() < deadline:
            self.get("/api/status")
            time.sleep(0.1)
        self.assertEqual(self.get("/api/status")["state"], "connected")

    def test_idle_without_any_request_disconnects(self) -> None:
        self.connect()
        self.get("/api/stop")
        # 绕开 /api/status 的 touch，直接冻结活动时间戳模拟页面被关掉
        self.console._last_activity = time.monotonic() - self.IDLE_TIMEOUT * 3
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.console.client.state.value == "disconnected":
                break
            time.sleep(0.05)
        self.assertEqual(self.console.client.state.value, "disconnected")
        self.assertEqual(self.robot.frames()[-1], STOP)

    def test_speed_frames(self) -> None:
        self.connect()
        self.get("/api/speed?side=left&value=60")
        self.get("/api/speed?side=right&value=0")
        frames = self.robot.wait_frames(3)
        # 通道按实车接线：left -> 0x02（ENB=物理左轮），right -> 0x01（ENA=物理右轮）
        self.assertEqual(frames[1], b"\xff\x02\x02\x3c\xff")
        self.assertEqual(frames[2], b"\xff\x02\x01\x00\xff")

    def test_speed_side_validated(self) -> None:
        self.connect()
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/speed?side=middle&value=50")
        self.assertEqual(ctx.exception.code, 400)

    def test_servo_frame(self) -> None:
        self.connect()
        self.get("/api/servo?n=3&a=120")
        self.assertEqual(self.robot.wait_frames(2)[1], b"\xff\x01\x03\x78\xff")

    def test_servo_angle_clamped_to_firmware_range(self) -> None:
        self.connect()
        self.get("/api/servo?n=1&a=999")
        self.assertEqual(self.robot.wait_frames(2)[1], b"\xff\x01\x01\xa0\xff")

    def test_gimbal_uses_real_car_channels(self) -> None:
        """2026-09-18 实车修正：水平轴走 7 号、垂直轴走 8 号，不是旧的 8/7。"""
        self.connect()
        self.get("/api/gimbal?pan=120")
        self.get("/api/gimbal?tilt=100")
        frames = self.robot.wait_frames(3)
        self.assertEqual(frames[1], b"\xff\x01\x07\x78\xff")
        self.assertEqual(frames[2], b"\xff\x01\x08\x64\xff")
        status = self.get("/api/status")
        self.assertEqual(status["gimbal"], {"pan": 120, "tilt": 100})

    def test_gimbal_travel_is_wider_than_firmware_servo_clamp(self) -> None:
        """实车行程 pan 0-185 / tilt 78-170，不能被 15-160 的通用舵机钳制截掉。"""
        self.connect()
        self.get("/api/gimbal?pan=999&tilt=-50")
        status = self.get("/api/status")
        self.assertEqual(status["gimbal"], {"pan": 185, "tilt": 78})
        self.assertEqual(self.robot.frames()[-1], b"\xff\x01\x08\x4e\xff")

    def test_gimbal_skips_unchanged_axis(self) -> None:
        self.connect()
        self.get("/api/gimbal?pan=100")
        before = len(self.robot.wait_frames(2))
        self.get("/api/gimbal?pan=100&tilt=90")
        self.assertEqual(len(self.robot.frames()), before)

    def test_gimbal_home_is_server_side(self) -> None:
        self.connect()
        self.get("/api/gimbal?pan=130&tilt=110")
        self.get("/api/gimbal_home")
        status = self.get("/api/status")
        self.assertEqual(status["home"], {"pan": 130, "tilt": 110})
        self.assertEqual(status["gimbal"], {"pan": 130, "tilt": 110})

    def test_light_frames(self) -> None:
        self.connect()
        self.get("/api/light?on=1")
        self.get("/api/light?on=0")
        frames = self.robot.wait_frames(3)
        self.assertEqual(frames[1], b"\xff\x04\x00\x00\xff")
        self.assertEqual(frames[2], b"\xff\x04\x01\x00\xff")

    def test_turn_frames_not_inverted(self) -> None:
        # 2026-09-19 定版：方向帧不互换（实车接线 A=右轮/B=左轮，固件
        # TurnLeft/Right 本身就是正确的物理转向）；只换速度通道。
        self.connect()
        self.get("/api/cmd?k=a")
        self.get("/api/cmd?k=d")
        frames = self.robot.wait_frames(3)
        self.assertEqual(frames[1], protocol.LEFT)
        self.assertEqual(frames[2], protocol.RIGHT)

    def test_logs_endpoint_returns_new_lines_only(self) -> None:
        self.connect()
        first = self.get("/api/logs?since=0")
        self.assertTrue(first["lines"])
        self.assertTrue(all("seq" in line for line in first["lines"]))
        again = self.get("/api/logs?since=%d" % first["seq"])
        self.assertEqual(again["lines"], [])

    def test_disconnect_stops_before_closing(self) -> None:
        self.connect()
        self.get("/api/cmd?k=w")
        self.get("/api/disconnect")
        frames = self.robot.wait_frames(3)
        self.assertEqual(frames[-1], STOP)
        self.assertEqual(self.get("/api/status")["state"], "disconnected")

    # -- 会话模型（所有权/抢占/续期语义） ----------------------------------

    def http_error_json(self, path: str):
        """发起请求并期望 HTTP 错误，返回 (状态码, 解析后的 JSON body)。"""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.raw(path)
        return ctx.exception.code, json.loads(ctx.exception.read().decode("utf-8"))

    def connect_as(self, sid: str) -> dict:
        """以指定 sid 连接并等待连上，返回 /api/status。"""
        status = self.get(
            "/api/connect?sid=%s&host=127.0.0.1&port=%d" % (sid, self.robot.port)
        )
        self.assertTrue(status["ok"])
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            status = self.get("/api/status?sid=%s" % sid)
            if status["state"] == "connected":
                return status
            time.sleep(0.02)
        self.fail("未能连上假机器人：%s" % status)

    def test_command_busy_for_other_active_session(self) -> None:
        """别的 sid 活跃持有时，/api/cmd 返回 busy:true 且 HTTP 423。"""
        self.connect_as("owner-a")
        code, body = self.http_error_json("/api/cmd?sid=owner-b&k=w")
        self.assertEqual(code, 423)
        self.assertTrue(body["busy"])
        self.assertFalse(body["ok"])

    def test_stop_from_non_owner_still_sends_stop(self) -> None:
        """/api/stop 对非 owner 也一定发 STOP（不受门控）。"""
        self.connect_as("owner-a")
        self.get("/api/cmd?sid=owner-a&k=w")
        result = self.get("/api/stop?sid=owner-b")
        self.assertTrue(result["ok"])
        self.assertEqual(self.robot.wait_frames(3)[-1], STOP)

    def test_claim_rejected_while_other_holds_motion(self) -> None:
        """/api/claim 在对方按住方向键（有未决运动指令）时抢不过来。"""
        self.connect_as("owner-a")
        self.get("/api/cmd?sid=owner-a&k=w")
        code, body = self.http_error_json("/api/claim?sid=owner-b")
        self.assertEqual(code, 423)
        self.assertTrue(body["busy"])

    def test_error_response_is_not_mislabeled_busy_423(self) -> None:
        """未连接等 _error 响应不该因 busy 字段被误标 423；真实原因要能读到。

        2026-09-25 实测坑：/api/route 在“未连接”时返回 423 busy，
        调用方（含验证脚本）会以为是被别的会话挡住。"""
        self.connect_as("owner-a")
        self.console.client.disconnect("测试断开")  # 底层断开，owner 还在
        code, raw = self.raw("/api/gimbal?sid=owner-a&pan=100")
        body = json.loads(raw.decode("utf-8"))
        self.assertEqual(code, 200)
        self.assertFalse(body["ok"])
        self.assertIn("未连接", body["reason"])

    def test_owner_idle_timeout_stops_and_disconnects(self) -> None:
        """owner 超过 session_timeout 无请求 → STOP + 断开 2001 + owner 清空。"""
        self.connect_as("owner-a")
        self.get("/api/cmd?sid=owner-a&k=w")
        self.console._last_activity = time.monotonic() - self.IDLE_TIMEOUT * 3
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if self.console.client.state.value == "disconnected":
                break
            time.sleep(0.05)
        self.assertEqual(self.console.client.state.value, "disconnected")
        self.assertEqual(self.robot.frames()[-1], STOP)
        self.assertIsNone(self.console._owner)

    def test_bystander_polling_does_not_renew_owner(self) -> None:
        """旁观者（不带 sid 或错误 sid）轮询 /api/status 不能给 owner 续期。"""
        self.connect_as("owner-a")
        frozen = time.monotonic() - self.IDLE_TIMEOUT + 0.5
        self.console._last_activity = frozen
        time.sleep(0.05)
        self.get("/api/status")
        self.get("/api/status?sid=owner-b")
        self.assertEqual(self.console._last_activity, frozen)


if __name__ == "__main__":
    unittest.main()
