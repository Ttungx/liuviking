#!/usr/bin/env python3
"""本地假机器人：模拟原厂 ``wifirobots.py`` 的 2001 服务 + 可选 MJPEG。

用途：电机/摄像头未接时，让 GUI、tools、测试在同一台 PC 上完成端到端联调。

用法::

    python tools/fake_robot.py                          # 127.0.0.1:2001 + MJPEG :8080
    python tools/fake_robot.py --mjpeg-port 0           # 只跑控制端口
    python tools/fake_robot.py --host 0.0.0.0           # 允许局域网连接

然后：

    python src/main.py --host 127.0.0.1 --connect \
        --video-url "http://127.0.0.1:8080/?action=stream"
"""

from __future__ import annotations

import argparse
import base64
import socketserver
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402

#: 1x1 白色 JPEG（仅用于联调视频链路）
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

_MOTION_NAMES = {
    0x00: "STOP",
    0x01: "FORWARD",
    0x02: "BACKWARD",
    0x03: "LEFT",
    0x04: "RIGHT",
}


def describe(frame: bytes) -> str:
    b0, b1, b2 = frame[1], frame[2], frame[3]
    if b0 == 0x00:
        return f"运动 {_MOTION_NAMES.get(b1, f'?0x{b1:02X}')}"
    if b0 == 0x02:
        side = "左侧" if b1 == protocol.SPEED_LEFT else "右侧"
        return f"速度 {side} {b2}%"
    if b0 == 0x01:
        return f"舵机 {b1} -> {b2}°"
    if b0 == 0x13:
        return f"模式 {b1}"
    if b0 == 0x04:
        return "开灯" if b1 == 0x00 else "关灯"
    if b0 == 0x05:
        return "读电压"
    if b0 == 0xEF:
        return "心跳"
    return "未知帧"


class RobotHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        accumulator = protocol.FrameAccumulator()
        print(f"[fake-robot] 客户端连接 {self.client_address[0]}:{self.client_address[1]}")
        try:
            while True:
                data = self.request.recv(1024)
                if not data:
                    break
                for frame in accumulator.feed(data):
                    print(
                        f"[fake-robot] {time.strftime('%H:%M:%S')} "
                        f"{protocol.packet_hex(frame)}  {describe(frame)}"
                    )
        except OSError as exc:
            print(f"[fake-robot] 连接异常: {exc}")
        finally:
            print(f"[fake-robot] 客户端断开 {self.client_address[0]}:{self.client_address[1]}")


class MJPEGHandler(BaseHTTPRequestHandler):
    fps = 5.0

    def do_GET(self) -> None:  # noqa: N802
        if "action=stream" in self.path:
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=boundarydonotcross"
            )
            self.end_headers()
            try:
                while True:
                    self.wfile.write(
                        b"--boundarydonotcross\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(TINY_JPEG)).encode() + b"\r\n\r\n"
                    )
                    self.wfile.write(TINY_JPEG)
                    self.wfile.write(b"\r\n")
                    time.sleep(1.0 / max(self.fps, 0.1))
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                b"<html><body><h3>fake-robot MJPEG</h3>"
                b"<p>stream: <code>/?action=stream</code></p></body></html>"
            )
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 静默 HTTP 访问日志
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="本地假机器人（原厂 2001 协议 + MJPEG）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址，0.0.0.0 允许局域网")
    parser.add_argument("--port", type=int, default=2001, help="控制端口，默认 2001")
    parser.add_argument("--mjpeg-port", type=int, default=8080, help="MJPEG 端口，0=关闭")
    parser.add_argument("--fps", type=float, default=5.0, help="MJPEG 帧率，默认 5")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    control = socketserver.ThreadingTCPServer((args.host, args.port), RobotHandler)
    control.daemon_threads = True
    threading.Thread(target=control.serve_forever, name="fake-control", daemon=True).start()
    print(f"[fake-robot] 控制服务已启动 tcp://{args.host}:{args.port}")

    mjpeg_server = None
    if args.mjpeg_port > 0:
        MJPEGHandler.fps = args.fps
        mjpeg_server = ThreadingHTTPServer((args.host, args.mjpeg_port), MJPEGHandler)
        mjpeg_server.daemon_threads = True
        threading.Thread(target=mjpeg_server.serve_forever, name="fake-mjpeg", daemon=True).start()
        print(
            f"[fake-robot] MJPEG 已启动 "
            f"http://{args.host}:{args.mjpeg_port}/?action=stream ({args.fps} fps)"
        )

    print("[fake-robot] Ctrl+C 退出")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[fake-robot] 正在退出…")
    finally:
        control.shutdown()
        control.server_close()
        if mjpeg_server is not None:
            mjpeg_server.shutdown()
            mjpeg_server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
