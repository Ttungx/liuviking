"""WASD 多键状态机。

需求（交接文档第 5 节 MVP 策略）：

- 最后按下的有效方向键优先；
- 松开当前键时，若另一个方向键仍按着，切换到该方向；
- 全部释放 -> 返回 None（调用方发 STOP）；
- 不能简单地"任何 keyUp 都 STOP"，否则多键同时按会误停。

纯逻辑实现，不依赖 Qt，便于单元测试。
"""

import time
from typing import List, Optional, Tuple

DRIVING_KEYS = ("w", "s", "a", "d")


class KeyStateMachine:
    """维护当前按下的方向键集合与优先级。"""

    def __init__(self) -> None:
        self._order = []

    @property
    def pressed(self) -> Tuple[str, ...]:
        """当前按住的键，按按下先后排序（最早 -> 最近）。"""
        return tuple(self._order)

    @property
    def active_key(self) -> Optional[str]:
        """当前生效的方向键：最近按下的那个；无按键时为 None。"""
        return self._order[-1] if self._order else None

    def press(self, key: str) -> Optional[str]:
        """按下方向键，返回按下后的生效方向键。

        重复按下（键盘自动重复被上层过滤，这里做幂等保护）不会改变优先级。
        """
        key = key.lower()
        if key not in DRIVING_KEYS:
            return self.active_key
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        return self.active_key

    def release(self, key: str) -> Optional[str]:
        """松开方向键，返回松开后的生效方向键（可能仍是另一个键）。"""
        key = key.lower()
        if key in self._order:
            self._order.remove(key)
        return self.active_key

    def clear(self) -> None:
        """清空全部按键状态（失焦/断线/急停时调用）。"""
        self._order.clear()


class MotionRefreshWatchdog:
    """运动刷新看门狗（纯逻辑，UI 层定时查询）。

    与手机页面同款的防线：按住方向键期间必须持续收到"活着"的信号（Qt 的
    按键自动重复事件），超过 :attr:`timeout` 秒没有刷新就判定按键状态失真
    （keyup 被拖动窗口/系统弹窗吞掉等），应当急停。

    系统关闭了按键重复时，按住 1.2s 后会被误判超时——这是有意的安全取舍：
    宁可"按住不持续走"，不可"松不开一直走"。
    """

    def __init__(self, timeout: float = 1.2) -> None:
        self.timeout = timeout
        self._deadline = None

    def arm(self, now: Optional[float] = None) -> None:
        """开始/继续监督（按下方向键或收到自动重复时调用）。"""
        self._deadline = (now if now is not None else time.monotonic()) + self.timeout

    def disarm(self) -> None:
        """停止监督（松开全部按键/急停后调用）。"""
        self._deadline = None

    @property
    def armed(self) -> bool:
        """是否正在监督一次未完成的运动。"""
        return self._deadline is not None

    def should_stop(self, now: Optional[float] = None) -> bool:
        """是否已经超时（调用方负责之后 disarm）。"""
        if self._deadline is None:
            return False
        return (now if now is not None else time.monotonic()) >= self._deadline
