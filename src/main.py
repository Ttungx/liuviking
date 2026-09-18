"""程序入口：``python src/main.py`` 或 ``python -m src.main``。"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from src.ui import MainWindow  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="小R科技 WiFi 小车 WASD 遥控客户端")
    parser.add_argument("--host", help="小车 IP（默认使用上次保存的配置）")
    parser.add_argument("--port", type=int, default=None, help="控制端口，默认 2001")
    parser.add_argument("--connect", action="store_true", help="启动后自动连接")
    parser.add_argument("--video-url", help="启动后自动启用视频，如 http://192.168.88.100:8080/?action=stream")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    parser.add_argument(
        "--smoke-test",
        type=float,
        default=0.0,
        help=argparse.SUPPRESS,  # 自动化冒烟测试：N 秒后自动退出
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("xiaor-remote")
    window = MainWindow(
        host=args.host,
        port=args.port,
        connect_on_start=args.connect,
        log_level=getattr(logging, args.log_level),
        video_url=args.video_url,
    )
    window.show()
    if args.smoke_test > 0:
        QTimer.singleShot(int(args.smoke_test * 1000), app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
