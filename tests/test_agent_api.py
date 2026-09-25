# -*- coding: utf-8 -*-
"""Agent 调试接口（/api/move、/api/telemetry、/api/claim lease）的最小结测。

只测"失败就说明逻辑坏了"的三件事：
- move 恰好发一帧 + 收尾 STOP，不被运动看门狗打脸
- telemetry 收得到样、且一定发退出帧（忘了退出车就只剩测距）
- _feed_rx 认跨包/粘包/垃圾字节的帧切分
"""

from __future__ import print_function

import os
import socket
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "pi"))  # noqa: E402

import web_controller as wc  # noqa: E402

STOP = b"\xff\x00\x00\x00\xff"
FORWARD = b"\xff\x00\x01\x00\xff"
SENSE_START = b"\xff\x13\x05\x00\xff"
SENSE_END = b"\xff\x13\x00\x00\xff"
DIST = b"\xff\x03\x00\x1a\xff"  # 26cm


class RelayRobot:
    """假固件：收到 13-05 就每 0.2s 回一帧距离，收到 13-00 停。"""

    def __init__(self):
        self.received = bytearray()
        self.lock = threading.Lock()
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                stop = threading.Event()
                reporter = None
                while True:
                    try:
                        data = self.request.recv(1024)
                    except OSError:
                        break
                    if not data:
                        break
                    with outer.lock:
                        outer.received.extend(data)
                    if SENSE_START in data and reporter is None:
                        reporter = threading.Thread(
                            target=self._report, args=(self.request, stop), daemon=True)
                        reporter.start()
                    elif SENSE_END in data:
                        stop.set()

            @staticmethod
            def _report(conn, stop):
                while not stop.wait(0.2):
                    try:
                        conn.sendall(DIST)
                    except OSError:
                        return

        self._server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def frames(data):
    out, middle, in_frame = [], [], False
    for byte in bytearray(data):
        if byte == 0xFF:
            if in_frame and len(middle) == 3:
                out.append(bytes([0xFF] + middle + [0xFF]))
            middle, in_frame = [], True
        elif in_frame:
            middle.append(byte)
            if len(middle) > 3:
                middle, in_frame = [], False
    return out


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.server = RelayRobot()
        self.server.start()
        self.bridge = wc.RobotBridge(
            host="127.0.0.1",
            port=self.server.port,
            session_timeout=30.0,
            heartbeat_interval=100.0,
            motion_timeout=1.2,
        )

    def tearDown(self):
        self.bridge.close()
        self.server.stop()

    def test_claim_lease_holds_control(self):
        status = self.bridge.claim("sid-a", 60)
        self.assertTrue(status["ok"], status)
        # lease 到未来时间：在 60s 内别的 sid 抢不动
        self.assertTrue(self.bridge._last_activity > time.time() + 50)
        other = self.bridge.claim("sid-b", 0)
        self.assertTrue(other.get("busy"), other)

    def _wait_frames(self, count, timeout=2.0):
        """发送到服务端 recv 是异步的，轮询等足帧数。"""
        deadline = time.monotonic() + timeout
        got = []
        while time.monotonic() < deadline:
            got = frames(bytes(self.server.received))
            if len(got) >= count:
                return got
            time.sleep(0.02)
        return got

    def test_move_sends_one_frame_then_stops(self):
        status = self.bridge.move("sid-a", "w", 300)
        self.assertTrue(status["ok"], status)
        got = self._wait_frames(3)
        self.assertEqual(got, [STOP, FORWARD, STOP])

    def test_telemetry_refused_without_force(self):
        result = self.bridge.sense("sid-a", 1.0)
        self.assertFalse(result["ok"])
        self.assertIn("force", result["reason"])

    def test_sense_collects_samples_and_exits_mode(self):
        result = self.bridge.sense("sid-a", 1.0, True)
        self.assertTrue(result["ok"], result)
        self.assertGreater(len(result["samples"]), 0)
        self.assertEqual(result["distance_cm"], 0x1A)
        # 发送到服务端 recv 有毫秒级异步，等退出帧落地
        deadline = time.monotonic() + 1.0
        got = []
        while time.monotonic() < deadline:
            got = frames(bytes(self.server.received))
            if got and got[-1] == SENSE_END:
                break
            time.sleep(0.05)
        self.assertIn(SENSE_START, got)
        self.assertEqual(got[-1], SENSE_END)  # 收样后必须退出，否则固件不收运动帧

    def test_feed_rx_handles_split_and_garbage(self):
        b = wc.RobotBridge.__new__(wc.RobotBridge)
        b._rx_buf = b""
        b._rx_frames = 0
        b._samples = None
        b._distance = None
        b._distance_at = 0.0
        for fr in b._feed_rx(b"\x00\xff\x03\x00") + b._feed_rx(b"\x1a\xff") \
                + b._feed_rx(b"\xff\x03\x00\x2a\xff"):
            b._note_rx_frame(fr)
        self.assertEqual(b._distance, 0x2A)
        self.assertEqual(b._rx_frames, 2)


class OccupancyTests(unittest.TestCase):
    """2001 单客户端：占用可见、排队不可见。"""

    def _write_proc_net(self, lines):
        handle, path = tempfile.mkstemp(prefix="procnettcp")
        with os.fdopen(handle, "w") as fh:
            fh.write("  sl  local_address rem_address   st\n")
            fh.writelines(lines)
        self.addCleanup(os.remove, path)
        return path

    def test_tcp_holders_skips_listen_other_ports_and_loopback(self):
        path = self._write_proc_net([
            "   0: 00000000:07D1 00000000:0000 0A x\n",     # 2001 LISTEN，忽略
            "   1: 00000000:0050 0A00000A:8B1F 01 x\n",     # 别的端口，忽略
            "   2: 0100007F:07D1 0100007F:B2E6 01 x\n",     # 回环幽灵（本桥自己），忽略
            "   3: 0100000A:07D1 48007D0A:8B1F 01 x\n",     # 10.125.0.72:35615 <- 真占用者
        ])
        self.assertEqual(wc._tcp_holders(2001, (path,)), ["10.125.0.72:35615"])

    def test_connect_reports_occupier_instead_of_queuing(self):
        bridge = wc.RobotBridge(
            host="127.0.0.1", port=2001, session_timeout=5.0, heartbeat_interval=100.0)
        orig = wc._tcp_holders
        wc._tcp_holders = lambda port, paths=None: ["10.60.121.72:8760"]
        try:
            status = bridge.command("sid-a", "w")
            self.assertFalse(status["ok"])
            self.assertIn("10.60.121.72:8760", status["reason"])
        finally:
            wc._tcp_holders = orig
            bridge.close()


if __name__ == "__main__":
    unittest.main()
