"""RobotClient 与本地假机器人服务器的端到端协议测试。

用 ``socketserver`` 起一个本机 TCP 服务冒充原厂 2001 端口，验证：
连接即 STOP、方向帧字节、断开前 STOP、心跳、错误状态、左右互换校准。
"""

from __future__ import annotations

import socket
import socketserver
import sys
import threading
import time
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402
from src.robot_client import ConnectionState, RobotClient  # noqa: E402


class FakeRobotServer:
    """记录所有收到字节的单客户端 TCP 服务器。"""

    def __init__(self) -> None:
        self._received = bytearray()
        self._lock = threading.Lock()
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
                    with outer._lock:
                        outer._received.extend(data)

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
        with self._lock:
            return protocol.parse_stream(bytes(self._received))

    def wait_frames(self, count: int, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.frames()
            if len(frames) >= count:
                return frames
            time.sleep(0.02)
        return self.frames()


class RobotClientTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeRobotServer()
        self.server.start()
        self.client = RobotClient()

    def tearDown(self):
        self.client.disconnect("测试清理")
        self.server.stop()

    def test_connect_sends_stop_first(self):
        self.assertTrue(self.client.connect("127.0.0.1", self.server.port, timeout=1.0))
        self.assertEqual(self.client.state, ConnectionState.CONNECTED)
        frames = self.server.wait_frames(1)
        self.assertEqual(frames[0], protocol.STOP)

    def test_direction_frames_reach_server(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_direction("w")
        self.client.send_direction("a")
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[1], protocol.FORWARD)
        self.assertEqual(frames[2], protocol.LEFT)

    def test_disconnect_sends_final_stop(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_direction("d")
        self.client.disconnect()
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[-1], protocol.STOP)
        self.assertEqual(self.client.state, ConnectionState.DISCONNECTED)

    def test_stop_command(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_direction("w")
        self.client.stop()
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[-1], protocol.STOP)

    def test_swap_left_right_calibration(self):
        self.client.swap_left_right = True
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_direction("a")
        self.client.send_direction("d")
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[1], protocol.RIGHT)
        self.assertEqual(frames[2], protocol.LEFT)

    def test_invalid_key_maps_to_stop(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_direction("q")
        frames = self.server.wait_frames(2)
        self.assertEqual(frames[1], protocol.STOP)

    def test_heartbeat_is_sent(self):
        self.client.HEARTBEAT_INTERVAL = 0.2
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        frames = self.server.wait_frames(2, timeout=3.0)
        self.assertIn(protocol.HEARTBEAT, frames)

    def test_speed_and_servo_commands(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        self.client.send_speed(protocol.SPEED_LEFT, 80)
        self.client.send_servo(1, 90)
        frames = self.server.wait_frames(3)
        self.assertEqual(frames[1], protocol.speed_command(protocol.SPEED_LEFT, 80))
        self.assertEqual(frames[2], protocol.servo_command(1, 90))

    def test_send_on_closed_socket_enters_error_state(self):
        self.client.connect("127.0.0.1", self.server.port, timeout=1.0)
        # 人为关闭底层 socket，模拟网络异常
        with self.client._send_lock:  # noqa: SLF001 - 测试内部状态
            self.client._sock.close()  # noqa: SLF001
        self.assertFalse(self.client.send_direction("w"))
        self.assertEqual(self.client.state, ConnectionState.ERROR)

    def test_connect_failure_sets_error_state(self):
        # 端口 1 几乎必然不可用
        self.assertFalse(self.client.connect("127.0.0.1", 1, timeout=0.5))
        self.assertEqual(self.client.state, ConnectionState.ERROR)

    def test_send_raw_rejects_invalid_frame(self):
        with self.assertRaises(ValueError):
            self.client.send_raw(b"\x00\x01\x02")


if __name__ == "__main__":
    unittest.main()
