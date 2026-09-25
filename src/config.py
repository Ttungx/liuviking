"""客户端配置持久化（默认 ``~/.xiaor_remote_config.json``）。"""

import json
import logging
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / ".xiaor_remote_config.json"

DEFAULTS = {
    "host": "192.168.88.100",
    "port": 2001,
    "host_ips": {},  # .local 名字 -> 最近一次解析成功的 IP（Windows mDNS 不稳时兜底）
    "gimbal_home": {"pan": 90, "tilt": 90},
    "stop_on_focus_loss": True,
    "left_speed": 100,
    "right_speed": 100,
}


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """读取配置；文件不存在或损坏时返回默认值。"""
    config = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            config.update({k: v for k, v in data.items() if k in DEFAULTS})
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning("读取配置失败 %s: %s，使用默认配置", path, exc)
    return config


def save_config(config: Dict[str, Any], path: Path = CONFIG_PATH) -> None:
    """保存配置；失败只记录日志，不影响运行。"""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("保存配置失败 %s: %s", path, exc)
