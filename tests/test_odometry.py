"""里程计与路线决策的纯逻辑测试（无网络、无机器人）。"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import odometry  # noqa: E402
from src.web_console import _parse_waypoints  # noqa: E402


class OdometryTests(unittest.TestCase):
    def test_straight_drive_distance(self):
        odo = odometry.Odometry(track_mm=100.0, max_speed_mm_per_s=200.0)
        left, right = odo.wheel_speeds("w", 50, 50)
        self.assertEqual((left, right), (100.0, 100.0))
        odo.integrate(left, right, 1.0)
        self.assertAlmostEqual(odo.x, 100.0)
        self.assertAlmostEqual(odo.y, 0.0)
        self.assertAlmostEqual(odo.theta, 0.0)

    def test_pivot_left_turns_ccw_in_place(self):
        odo = odometry.Odometry(track_mm=100.0, max_speed_mm_per_s=200.0)
        odo.integrate(*odo.wheel_speeds("a", 100, 100), 0.1)
        self.assertAlmostEqual(odo.x, 0.0)
        self.assertAlmostEqual(odo.y, 0.0)
        self.assertGreater(odo.theta, 0.3)

    def test_arc_right_curves_right(self):
        odo = odometry.Odometry(track_mm=100.0, max_speed_mm_per_s=200.0)
        odo.integrate(*odo.wheel_speeds("w", 100, 50), 0.2)  # 右轮慢 -> 顺时针（右转）
        self.assertLess(odo.theta, 0.0)
        self.assertGreater(odo.x, 0.0)

    def test_stop_has_no_motion(self):
        odo = odometry.Odometry()
        self.assertEqual(odo.wheel_speeds(None, 100, 100), (0.0, 0.0))
        self.assertEqual(odo.wheel_speeds("x", 100, 100), (0.0, 0.0))

    def test_duty_clamped(self):
        odo = odometry.Odometry(max_speed_mm_per_s=200.0)
        self.assertEqual(odo.wheel_speeds("w", 250, -5), (200.0, 0.0))

    def test_wheel_calibration_scales_each_side(self):
        odo = odometry.Odometry(
            max_speed_mm_per_s=200.0,
            left_scale=0.8,
            right_scale=1.1,
        )
        self.assertAlmostEqual(odo.wheel_speeds("w", 50, 50)[0], 80.0)
        self.assertAlmostEqual(odo.wheel_speeds("w", 50, 50)[1], 110.0)

    def test_deadzone_is_removed_from_effective_duty(self):
        odo = odometry.Odometry(max_speed_mm_per_s=200.0, left_deadzone=20.0)
        self.assertEqual(odo.wheel_speeds("w", 20, 50)[0], 0.0)
        self.assertAlmostEqual(odo.wheel_speeds("w", 20, 50)[1], 100.0)

    def test_wrap_angle(self):
        self.assertAlmostEqual(odometry.wrap_angle(0.5), 0.5)
        self.assertAlmostEqual(odometry.wrap_angle(3 * math.pi), math.pi)
        self.assertAlmostEqual(odometry.wrap_angle(-3 * math.pi), math.pi)

    def test_path_capped(self):
        odo = odometry.Odometry(track_mm=100.0, max_speed_mm_per_s=1000.0)
        for _ in range(odometry.PATH_MAX_POINTS + 50):
            odo.integrate(500.0, 500.0, 0.1)  # 每步 50mm
        self.assertLessEqual(len(odo.path), odometry.PATH_MAX_POINTS)
        self.assertGreater(len(odo.path), odometry.PATH_MAX_POINTS - 5)

    def test_reset_clears_pose_and_path(self):
        odo = odometry.Odometry(max_speed_mm_per_s=200.0)
        odo.integrate(100.0, 100.0, 1.0)
        odo.reset()
        self.assertEqual((odo.x, odo.y, odo.theta), (0.0, 0.0, 0.0))
        self.assertEqual(odo.path, [(0.0, 0.0)])


class RouteCommandTests(unittest.TestCase):
    def test_target_behind_forces_pivot_left(self):
        cmd = odometry.route_command(-100.0, 0.0, 0.0)
        self.assertTrue(cmd["pivot"])
        self.assertEqual(cmd["key"], "a")

    def test_target_behind_on_right_forces_pivot_right(self):
        cmd = odometry.route_command(-100.0, 0.0, -0.5)  # 航向偏差约 -151°
        self.assertTrue(cmd["pivot"])
        self.assertEqual(cmd["key"], "d")

    def test_small_left_error_uses_arc_with_left_inner(self):
        cmd = odometry.route_command(100.0, 10.0, 0.0)
        self.assertFalse(cmd["pivot"])
        self.assertEqual(cmd["key"], "w")
        self.assertLess(cmd["left"], cmd["right"])

    def test_small_right_error_uses_arc_with_right_inner(self):
        cmd = odometry.route_command(100.0, -10.0, 0.0)
        self.assertFalse(cmd["pivot"])
        self.assertGreater(cmd["left"], cmd["right"])

    def test_inner_speed_bounded(self):
        cmd = odometry.route_command(100.0, 34.0, 0.0)  # 误差 18.8°，接近原地阈值
        self.assertGreaterEqual(cmd["left"], 58)
        self.assertLessEqual(cmd["right"], 100)


class PolylineCommandTests(unittest.TestCase):
    @staticmethod
    def _segment_distance(x, y, start, end):
        sx, sy = start
        ex, ey = end
        dx, dy = ex - sx, ey - sy
        length_sq = dx * dx + dy * dy
        if not length_sq:
            return math.hypot(x - sx, y - sy)
        t = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / length_sq))
        return math.hypot(x - (sx + t * dx), y - (sy + t * dy))

    def test_sharp_turn_uses_bounded_pivot_speed(self):
        cmd = odometry.polyline_command(
            0.0, 0.0, 0.0, (0.0, 0.0), (0.0, 500.0)
        )
        self.assertTrue(cmd["pivot"])
        self.assertEqual(cmd["key"], "a")
        self.assertEqual((cmd["left"], cmd["right"]), (45, 45))

    def test_cross_track_error_turns_back_to_segment(self):
        cmd = odometry.polyline_command(
            100.0, 10.0, 0.0, (0.0, 0.0), (500.0, 0.0), allow_arc=True
        )
        self.assertFalse(cmd["pivot"])
        self.assertEqual(cmd["key"], "w")
        self.assertGreater(cmd["left"], cmd["right"])

    def test_straight_segment_does_not_bend_from_cross_track_error(self):
        cmd = odometry.polyline_command(
            100.0, 10.0, 0.0, (0.0, 0.0), (500.0, 0.0)
        )
        self.assertFalse(cmd["pivot"])
        self.assertEqual((cmd["left"], cmd["right"]), (100, 100))

    def test_straight_segment_corrects_large_cross_track_error(self):
        cmd = odometry.polyline_command(
            100.0, 45.0, 0.0, (0.0, 0.0), (1000.0, 0.0),
            pivot_deg=4.0, pivot_duty=30, min_inner=90,
        )
        self.assertFalse(cmd["pivot"])
        self.assertEqual(cmd["key"], "w")
        self.assertEqual((cmd["left"], cmd["right"]), (100, 0))
        self.assertEqual(cmd["turn_mode"], "one_wheel")

    def test_arc_turn_keeps_both_wheels_powered(self):
        cmd = odometry.polyline_command(
            0.0, 0.0, 0.0, (0.0, 0.0), (100.0, 50.0),
            allow_arc=True, pivot_deg=4.0, min_inner=70, turn_mode="arc",
        )
        self.assertFalse(cmd["pivot"])
        self.assertGreater(cmd["left"], 0)
        self.assertGreater(cmd["right"], 0)
        self.assertEqual(cmd["turn_mode"], "arc")

    def test_cross_track_correction_keeps_a_long_line_within_50mm(self):
        odo = odometry.Odometry(track_mm=120.0, max_speed_mm_per_s=250.0)
        odo.reset(y=45.0)
        max_error = 45.0
        for _ in range(500):
            cmd = odometry.polyline_command(
                odo.x, odo.y, odo.theta, (0.0, 0.0), (1000.0, 0.0),
                pivot_deg=4.0, pivot_duty=30, min_inner=90,
            )
            odo.integrate(
                *odo.wheel_speeds(cmd["key"], cmd["left"], cmd["right"]), 0.05
            )
            max_error = max(max_error, abs(odo.y))
            if odo.x >= 1000.0:
                break
        self.assertLessEqual(max_error, 50.0)

    def test_bounded_pivot_does_not_jump_a_large_angle_in_one_tick(self):
        odo = odometry.Odometry(track_mm=120.0, max_speed_mm_per_s=250.0)
        cmd = odometry.polyline_command(
            0.0, 0.0, 0.0, (0.0, 0.0), (0.0, 500.0)
        )
        odo.integrate(*odo.wheel_speeds(cmd["key"], cmd["left"], cmd["right"]), 0.05)
        self.assertLess(abs(odo.theta), math.radians(8.0))

    def test_rectangle_simulation_stays_near_planned_segments(self):
        route = [(500.0, 0.0), (500.0, -200.0), (0.0, -200.0), (0.0, 0.0)]
        odo = odometry.Odometry(track_mm=120.0, max_speed_mm_per_s=250.0)
        curved = not odometry.route_has_sharp_corners(route)
        index = 0
        max_error = 0.0
        for _ in range(1600):
            if index >= len(route):
                break
            target = route[index]
            if math.hypot(target[0] - odo.x, target[1] - odo.y) <= 12.0:
                index += 1
                continue
            start = (0.0, 0.0) if index == 0 else route[index - 1]
            cmd = odometry.polyline_command(
                odo.x,
                odo.y,
                odo.theta,
                start,
                target,
                allow_arc=curved,
                pivot_deg=10.0 if curved else 4.0,
                pivot_duty=45 if curved else 30,
                min_inner=90,
            )
            odo.integrate(
                *odo.wheel_speeds(cmd["key"], cmd["left"], cmd["right"]), 0.05
            )
            max_error = max(
                max_error,
                self._segment_distance(odo.x, odo.y, start, target),
            )
        self.assertEqual(index, len(route))
        self.assertLess(max_error, 25.0)


class SmoothRouteTests(unittest.TestCase):
    def test_two_point_route_stays_a_straight_line(self):
        route = odometry.smooth_route([(500.0, 0.0)])
        self.assertEqual(route, [(500.0, 0.0)])

    def test_collinear_mouse_samples_collapse_to_one_straight_segment(self):
        route = odometry.smooth_route([(100.0, 0.0), (200.0, 0.0), (500.0, 0.0)])
        self.assertEqual(route, [(500.0, 0.0)])

    def test_start_jitter_is_removed_before_route_planning(self):
        route = odometry.smooth_route([(-15.0, 13.0), (2395.0, -13.0), (2412.0, -609.0)])
        self.assertEqual(route[0], (2395.0, -13.0))

    def test_sharp_corner_route_keeps_the_planned_corner(self):
        route = odometry.smooth_route([(500.0, 0.0), (500.0, 200.0), (0.0, 200.0)])
        self.assertEqual(route, [(500.0, 0.0), (500.0, 200.0), (0.0, 200.0)])
        self.assertTrue(odometry.route_has_sharp_corners(route))

    def test_gently_curved_route_is_smoothed(self):
        route = odometry.smooth_route(
            [(34.7, 3.0), (68.4, 12.1), (100.0, 26.8), (128.6, 46.8),
             (153.2, 71.4), (173.2, 100.0), (187.9, 131.6), (197.0, 165.3),
             (200.0, 200.0)]
        )
        self.assertEqual(route[-1], (200.0, 200.0))
        self.assertGreater(len(route), 8)
        self.assertFalse(odometry.route_has_sharp_corners(route))


class ParseWaypointsTests(unittest.TestCase):
    def test_semicolon_and_newline(self):
        self.assertEqual(_parse_waypoints("100,0;100,200"), [(100.0, 0.0), (100.0, 200.0)])
        self.assertEqual(_parse_waypoints("100 0\n100 200"), [(100.0, 0.0), (100.0, 200.0)])

    def test_comments_and_blanks_ignored(self):
        self.assertEqual(_parse_waypoints("# 起点\n\n50,60\n"), [(50.0, 60.0)])

    def test_empty_returns_empty(self):
        self.assertEqual(_parse_waypoints(""), [])

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            _parse_waypoints("abc")
        with self.assertRaises(ValueError):
            _parse_waypoints("1,x")


if __name__ == "__main__":
    unittest.main()
