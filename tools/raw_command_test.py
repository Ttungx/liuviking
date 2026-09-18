#!/usr/bin/env python3
"""协议验证工具：对实车发单个方向命令，到时自动 STOP。

用法::

    python tools/raw_command_test.py --host 192.168.88.100 --command forward
    python tools/raw_command_test.py --host 192.168.88.100 --command all --duration 0.2

安全：
- 连接成功后先发 STOP；
- 非 stop 命令在 ``--duration`` 秒后自动 STOP（除非 ``--no-auto-stop``）；
- 断开前始终 STOP；
- 打印每一帧的 packet hex。
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402
from src.robot_client import RobotClient  # noqa: E402

DEFAULT_HOST = "192.168.88.100"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小R科技小车 TCP 协议验证工具")
    parser.add_argument("--host", default=DEFAULT_HOST, help="小车 IP")
    parser.add_argument("--port", type=int, default=2001, help="控制端口，默认 2001")
    parser.add_argument(
        "--command",
        default="forward",
        choices=["stop", "forward", "backward", "left", "right", "all"],
        help="要验证的命令；all 依次验证 停/前/后/左/右",
    )
    parser.add_argument("--duration", type=float, default=0.2, help="每个非停命令的脉冲时长（秒）")
    parser.add_argument("--timeout", type=float, default=2.0, help="连接超时（秒）")
    parser.add_argument("--no-auto-stop", action="store_true", help="到时后不自动 STOP（危险，仅诊断用）")
    parser.add_argument("--verbose", action="store_true", help="打印 DEBUG 日志")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s.%(msecs)03d %(message)s",
        datefmt="%H:%M:%S",
    )

    client = RobotClient()
    print(f"[probe] 连接 {args.host}:{args.port} ...")
    if not client.connect(args.host, args.port, timeout=args.timeout):
        print(f"[probe] 连接失败：{client.detail}")
        return 1
    print("[probe] 已连接（已自动发送 STOP）")

    names = (
        ["stop", "forward", "backward", "left", "right"]
        if args.command == "all"
        else [args.command]
    )

    exit_code = 0
    try:
        for name in names:
            packet = protocol.MOTION_BY_NAME[name]
            if not client.send_raw(packet, note=f"raw test {name}"):
                print(f"[probe] 发送 {name} 失败，停止测试")
                exit_code = 1
                break
            print(f"[probe] 发送 {name:>8}: {protocol.packet_hex(packet)}  ({packet.hex()})")

            if name == "stop":
                continue
            time.sleep(max(args.duration, 0.0))
            if not args.no_auto_stop:
                client.stop("到时自动 STOP")
                print(f"[probe] 自动 STOP: {protocol.packet_hex(protocol.STOP)}")
    except KeyboardInterrupt:
        print("\n[probe] 用户中断")
    finally:
        if client.is_connected:
            client.disconnect("测试结束")
            print("[probe] 断开前已发送 STOP 并关闭连接")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
