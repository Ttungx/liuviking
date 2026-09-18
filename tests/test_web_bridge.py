"""手机网页遥控桥的单元与 HTTP 集成测试（Python 3 运行，代码兼容 2.7）。"""

from __future__ import annotations

import json
import socket
import socketserver
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

sys.path.insert(0, str(PROJECT_ROOT / "pi"))  # noqa: E402

import web_controller as wc  # noqa: E402

STOP = b"\xff\x00\x00\x00\xff"
FORWARD = b"\xff\x00\x01\x00\xff"
LEFT = b"\xff\x00\x03\x00\xff"
HEARTBEAT = b"\xff\xef\xef\xee\xff"


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

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def frames(self):
        with self.lock:
            return wc_packets(bytes(self.received))

    def wait_frames(self, count, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.frames()
            if len(frames) >= count:
                return frames
            time.sleep(0.02)
        return self.frames()


def wc_packets(data: bytes):
    """按 FF 起止符提取 5 字节帧。"""
    frames = []
    middle = []
    in_frame = False
    for byte in data:
        if byte == 0xFF:
            if in_frame and len(middle) == 3:
                frames.append(bytes([0xFF] + middle + [0xFF]))
            middle = []
            in_frame = True
        elif in_frame:
            middle.append(byte)
            if len(middle) > 3:
                in_frame = False
                middle = []
    return frames


class RobotBridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeRobotServer()
        self.server.start()
        self.bridge = wc.RobotBridge(
            host="127.0.0.1",
            port=self.server.port,
            session_timeout=5.0,
            heartbeat_interval=10.0,
        )

    def tearDown(self):
        self.bridge.close()
        self.server.stop()

    def test_command_connects_and_sends_stop_then_motion(self):
        result = self.bridge.command("sid-a", "w")
        self.assertTrue(result["ok"])
        self.assertEqual(result["robot"], "connected")
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[0], STOP)
        self.assertEqual(frames[1], FORWARD)

    def test_stop_sends_stop_frame(self):
        self.bridge.command("sid-a", "d")
        self.bridge.stop("sid-a")
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[-1], STOP)

    def test_speed_frames(self):
        self.bridge.speed("sid-a", "left", 0)
        self.bridge.speed("sid-a", "right", 100)
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[1], b"\xff\x02\x01\x00\xff")
        self.assertEqual(frames[2], b"\xff\x02\x02\x64\xff")

    def test_servo_frames(self):
        self.bridge.servo("sid-a", 1, 90)
        self.bridge.servo("sid-a", 2, 15)
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[1], b"\xff\x01\x01\x5a\xff")
        self.assertEqual(frames[2], b"\xff\x01\x02\x0f\xff")

    def test_invalid_key_raises(self):
        with self.assertRaises(ValueError):
            self.bridge.command("sid-a", "x")

    def test_second_session_is_busy(self):
        self.bridge.command("sid-a", "w")
        with self.assertRaises(wc.BusyError):
            self.bridge.command("sid-b", "w")

    def test_non_owner_can_emergency_stop(self):
        self.bridge.command("sid-a", "w")
        result = self.bridge.stop("sid-b")  # 任何会话都有急停权
        self.assertTrue(result["ok"])
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[-1], STOP)

    def test_claim_takes_over_when_no_active_motion(self):
        self.bridge.command("sid-a", "w")
        self.bridge.stop("sid-a")
        result = self.bridge.claim("sid-b")
        self.assertTrue(result["ok"])
        self.assertFalse(result["busy"])
        self.assertTrue(self.bridge.command("sid-b", "a")["ok"])

    def test_claim_refused_while_owner_active(self):
        self.bridge.command("sid-a", "w")
        result = self.bridge.claim("sid-b")
        self.assertFalse(result["ok"])
        self.assertTrue(result["busy"])
        self.assertTrue(self.bridge.command("sid-a", "s")["ok"])

    def test_watchdog_stops_and_disconnects(self):
        self.bridge.session_timeout = 0.3
        self.bridge.command("sid-a", "w")
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[1], FORWARD)
        # 超时后看门狗应发送 STOP 并断开
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            frames = self.server.frames()
            if len(frames) >= 3 and frames[-1] == STOP:
                break
            time.sleep(0.05)
        self.assertEqual(frames[-1], STOP)
        status = self.bridge.status()
        self.assertEqual(status["robot"], "disconnected")
        self.assertIsNone(status["last"])

    def test_ping_keeps_session_alive(self):
        self.bridge.session_timeout = 0.3
        self.bridge.command("sid-a", "w")
        for _ in range(4):
            time.sleep(0.1)
            self.bridge.ping("sid-a")
        self.assertEqual(self.bridge.status()["robot"], "connected")

    def test_motion_timeout_stops_but_stays_connected(self):
        # 运动命令超过 motion_timeout 没刷新 -> 自动 STOP + 恢复默认速度，但连接保留
        self.bridge.motion_timeout = 0.3
        self.bridge.session_timeout = 30.0
        self.bridge.command("sid-a", "w")
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[1], FORWARD)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            frames = self.server.frames()
            if len(frames) >= 5:
                break
            time.sleep(0.05)
        self.assertEqual(frames[2], STOP)
        # 弧线转向可能留下不对称速度，超时停车后必须恢复 100/100
        self.assertEqual(frames[3], b"\xff\x02\x01\x64\xff")
        self.assertEqual(frames[4], b"\xff\x02\x02\x64\xff")
        status = self.bridge.status()
        self.assertEqual(status["robot"], "connected")  # 只停运动，不断开
        self.assertIsNone(status["last"])

    def test_stop_without_pending_motion_does_not_connect(self):
        # 桥没有未决运动时，/stop 不应抢占 2001（可能 GUI/官方 APP 在用）
        result = self.bridge.stop("sid-a")
        self.assertTrue(result["ok"])
        self.assertEqual(result["robot"], "disconnected")
        time.sleep(0.2)
        self.assertEqual(self.server.frames(), [])

    def test_stop_reconnects_when_motion_pending(self):
        # 有锁存运动而连接断开（如心跳失败路径）时，急停必须重连送达
        self.bridge.command("sid-a", "w")
        self.server.wait_frames(2)
        with self.bridge._lock:  # noqa: SLF001 - 模拟连接静默断开、运动仍锁存
            sock = self.bridge._sock
            self.bridge._sock = None
            sock.close()
        result = self.bridge.stop("sid-a")
        self.assertTrue(result["ok"])
        frames = self.server.wait_frames(4)  # 重连STOP + FORWARD + 急停STOP ×2
        self.assertEqual(frames[-1], STOP)
        self.assertIn(FORWARD, frames)

    def test_motion_refresh_keeps_moving(self):
        # 页面按住期间每 0.4s 重复 /cmd 刷新，运动不超时
        self.bridge.motion_timeout = 0.5
        self.bridge.session_timeout = 30.0
        self.bridge.command("sid-a", "w")
        time.sleep(0.2)
        self.bridge.command("sid-a", "w")
        time.sleep(0.2)
        self.assertEqual(self.bridge.status()["last"], "w")
        frames = self.server.frames()
        self.assertGreaterEqual(frames.count(FORWARD), 2)
        self.assertNotIn(STOP, frames[1:])  # 刷新期间绝不出现 STOP

    def test_reconnect_after_close(self):
        self.bridge.command("sid-a", "w")
        self.bridge.stop("sid-a")
        with self.bridge._lock:  # noqa: SLF001 - 模拟看门狗超时断开
            self.bridge._close_locked(True)  # noqa: SLF001
        result = self.bridge.command("sid-a", "a")
        self.assertTrue(result["ok"])
        frames = self.server.wait_frames(6)
        self.assertIn(LEFT, frames)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeRobotServer()
        self.server.start()
        self.bridge = wc.RobotBridge(
            host="127.0.0.1",
            port=self.server.port,
            session_timeout=5.0,
        )
        self.httpd = wc.make_server(self.bridge, "127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.bridge.close()
        self.server.stop()

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, response.read().decode("utf-8")

    def test_index_page(self):
        status, body = self.get("/?sid=x")
        self.assertEqual(status, 200)
        self.assertIn("小车遥控", body)
        self.assertIn("STOP", body)

    def test_status_endpoint(self):
        status, body = self.get("/status")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["robot"], "disconnected")

    def test_cmd_and_stop_endpoints(self):
        status, body = self.get("/cmd?k=w&sid=phone1")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[0], STOP)
        self.assertEqual(frames[1], FORWARD)

        status, body = self.get("/stop?sid=phone1")
        self.assertEqual(status, 200)
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[-1], STOP)

    def test_busy_returns_423(self):
        self.get("/cmd?k=w&sid=phone1")
        try:
            self.get("/cmd?k=w&sid=phone2")
            self.fail("expected HTTP 423")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 423)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertTrue(payload["busy"])

    def test_invalid_key_returns_400(self):
        try:
            self.get("/cmd?k=q&sid=phone1")
            self.fail("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_claim_endpoint(self):
        self.get("/cmd?k=w&sid=phone1")
        self.get("/stop?sid=phone1")
        status, body = self.get("/claim?sid=phone2")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_servo_endpoint(self):
        status, body = self.get("/servo?n=1&a=90&sid=phone1")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[1], b"\xff\x01\x01\x5a\xff")


if __name__ == "__main__":
    unittest.main()
