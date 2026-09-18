"""顶层入口（打包 exe / 直接运行）：``python app.py``。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
