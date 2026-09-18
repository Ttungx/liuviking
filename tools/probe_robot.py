#!/usr/bin/env python3
"""现场探测工具：TCP 2001 连通性、心跳、MJPEG 端口。

只读探测为主；唯一会发送的是一条 STOP（连接后默认安全动作）。

用法::

    python tools/probe_robot.py --host 192.168.88.100
    python tools/probe_robot.py --host 192.168.88.100 --heartbeat-seconds 12 --video
    python tools/probe_robot.py --host 192.168.88.100 --json
"""

from __future__ import annotations

import argparse
import http.client
import json
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402

DEFAULT_HOST = "192.168.88.100"
DEFAULT_VIDEO_PORTS = (8080, 8081)


def tcp_probe(host: str, port: int, timeout: float) -> Dict:
    started = time.monotonic()
    result: Dict = {"host": host, "port": port, "open": False, "detail": ""}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(protocol.STOP)
            result["open"] = True
            result["latency_ms"] = round((time.monotonic() - started) * 1000)
            result["detail"] = "连接成功，已发送 STOP"
    except OSError as exc:
        result["detail"] = f"连接失败: {exc}"
    return result


def heartbeat_probe(host: str, port: int, seconds: float, interval: float, timeout: float) -> Dict:
    """连接后持续发心跳，统计发送数量。"""
    result: Dict = {"seconds": seconds, "interval": interval, "sent": 0, "ok": False}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(protocol.STOP)
            result["sent"] = 1
            deadline = time.monotonic() + max(seconds, 0.0)
            while time.monotonic() < deadline:
                time.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
                if time.monotonic() >= deadline:
                    break
                sock.sendall(protocol.HEARTBEAT)
                result["sent"] += 1
            sock.sendall(protocol.STOP)
            result["sent"] += 1
            result["ok"] = True
    except OSError as exc:
        result["detail"] = f"心跳探测失败: {exc}"
    return result


def video_probe(host: str, port: int, timeout: float) -> Dict:
    result: Dict = {"port": port, "url": f"http://{host}:{port}/?action=stream", "streaming": False}
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("GET", "/?action=stream")
        response = conn.getresponse()
        chunk = response.read(1024)
        result["status"] = response.status
        result["bytes_received"] = len(chunk)
        result["streaming"] = response.status == 200 and len(chunk) > 0
        if result["streaming"]:
            result["detail"] = f"HTTP {response.status}，已收到 MJPEG 数据"
        else:
            result["detail"] = f"HTTP {response.status}，无有效数据（摄像头可能未接）"
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        result["detail"] = f"连接失败: {exc}"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小R科技小车现场探测")
    parser.add_argument("--host", default=DEFAULT_HOST, help="小车 IP")
    parser.add_argument("--port", type=int, default=2001, help="控制端口，默认 2001")
    parser.add_argument("--timeout", type=float, default=2.0, help="连接超时（秒）")
    parser.add_argument("--heartbeat-seconds", type=float, default=0.0, help="持续心跳秒数（0=只做连通性）")
    parser.add_argument("--heartbeat-interval", type=float, default=10.0, help="心跳间隔（秒），默认 10")
    parser.add_argument("--video", action="store_true", help="探测 MJPEG 端口 8080/8081")
    parser.add_argument("--video-ports", default=",".join(map(str, DEFAULT_VIDEO_PORTS)), help="视频端口列表")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report: Dict = {"control": tcp_probe(args.host, args.port, args.timeout)}

    if args.heartbeat_seconds > 0:
        report["heartbeat"] = heartbeat_probe(
            args.host, args.port, args.heartbeat_seconds, args.heartbeat_interval, args.timeout
        )

    if args.video:
        ports: List[int] = [int(p) for p in str(args.video_ports).split(",") if p.strip()]
        report["video"] = [video_probe(args.host, port, args.timeout) for port in ports]

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        control = report["control"]
        mark = "OK " if control["open"] else "FAIL"
        print(f"[{mark}] TCP {control['host']}:{control['port']}  {control['detail']}")
        if "heartbeat" in report:
            hb = report["heartbeat"]
            mark = "OK " if hb["ok"] else "FAIL"
            print(f"[{mark}] 心跳 {hb['seconds']}s：发送 {hb['sent']} 帧 {hb.get('detail', '')}")
        for video in report.get("video", []):
            mark = "OK " if video["streaming"] else "FAIL"
            print(f"[{mark}] 视频 :{video['port']}  {video.get('detail', '')}")

    return 0 if report["control"]["open"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
