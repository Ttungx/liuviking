"""WASD 多键状态机单元测试（交接文档第 5 节 MVP 策略）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.keyboard_controller import KeyStateMachine, MotionRefreshWatchdog  # noqa: E402


class KeyStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.machine = KeyStateMachine()

    def test_initial_state(self):
        self.assertIsNone(self.machine.active_key)
        self.assertEqual(self.machine.pressed, ())

    def test_single_key_press_release(self):
        self.assertEqual(self.machine.press("w"), "w")
        self.assertEqual(self.machine.release("w"), None)

    def test_last_pressed_has_priority(self):
        self.machine.press("w")
        self.assertEqual(self.machine.press("a"), "a")
        self.assertEqual(self.machine.press("d"), "d")

    def test_release_top_falls_back_to_previous(self):
        self.machine.press("w")
        self.machine.press("a")
        self.assertEqual(self.machine.release("a"), "w")

    def test_release_middle_keeps_top(self):
        self.machine.press("w")
        self.machine.press("a")
        self.machine.press("d")
        self.assertEqual(self.machine.release("a"), "d")

    def test_release_all_returns_none(self):
        self.machine.press("w")
        self.machine.press("s")
        self.machine.release("s")
        self.assertIsNone(self.machine.release("w"))

    def test_multi_key_does_not_falsely_stop(self):
        """W+A 同时按住，先松 A 应回到 W，而不是 STOP。"""
        self.machine.press("w")
        self.machine.press("a")
        self.assertEqual(self.machine.release("a"), "w")
        self.assertEqual(self.machine.release("w"), None)

    def test_repeat_press_keeps_single_entry(self):
        self.machine.press("w")
        self.machine.press("w")
        self.assertEqual(self.machine.pressed, ("w",))
        self.assertEqual(self.machine.press("w"), "w")

    def test_repeat_press_reorders_priority(self):
        self.machine.press("w")
        self.machine.press("a")
        self.machine.press("w")
        self.assertEqual(self.machine.pressed, ("a", "w"))
        self.assertEqual(self.machine.release("w"), "a")

    def test_uppercase_accepted(self):
        self.assertEqual(self.machine.press("W"), "w")
        self.assertEqual(self.machine.release("W"), None)

    def test_non_driving_keys_ignored(self):
        self.machine.press("w")
        self.assertEqual(self.machine.press("x"), "w")
        self.assertEqual(self.machine.release("space"), "w")

    def test_release_unpressed_key_is_noop(self):
        self.machine.press("w")
        self.assertEqual(self.machine.release("a"), "w")

    def test_clear(self):
        self.machine.press("w")
        self.machine.press("d")
        self.machine.clear()
        self.assertIsNone(self.machine.active_key)
        self.assertEqual(self.machine.pressed, ())


class MotionRefreshWatchdogTests(unittest.TestCase):
    """运动刷新看门狗（防 keyup 被吞导致跑飞）。"""

    def test_not_armed_never_stops(self):
        watchdog = MotionRefreshWatchdog(timeout=1.2)
        self.assertFalse(watchdog.should_stop(now=100.0))

    def test_times_out_after_timeout_without_refresh(self):
        watchdog = MotionRefreshWatchdog(timeout=1.2)
        watchdog.arm(now=10.0)
        self.assertFalse(watchdog.should_stop(now=11.0))
        self.assertTrue(watchdog.should_stop(now=11.3))

    def test_refresh_extends_deadline(self):
        watchdog = MotionRefreshWatchdog(timeout=1.2)
        watchdog.arm(now=10.0)
        watchdog.arm(now=10.9)  # 自动重复刷新
        self.assertFalse(watchdog.should_stop(now=11.5))
        self.assertTrue(watchdog.should_stop(now=12.2))

    def test_disarm_stops_supervision(self):
        watchdog = MotionRefreshWatchdog(timeout=1.2)
        watchdog.arm(now=10.0)
        watchdog.disarm()
        self.assertFalse(watchdog.should_stop(now=99.0))


if __name__ == "__main__":
    unittest.main()
