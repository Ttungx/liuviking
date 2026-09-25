"""开环里程计与路线跟随计算（纯逻辑，便于单测）。

小车没有可用的编码器（轮速寄存器回读恒 0），只能按"指令占空比 × 标定满速"
积分推算位姿；轮径 65mm 只用于换算轮子周长（显示用）。

ponytail: 开环推算——打滑、负载、电压跌落都会积累误差；升级路径=编码器或
摄像头闭环。坐标系：x 向右、y 向上，theta 逆时针为正（0 = 朝 +x），mm/rad。
"""

import math

WHEEL_DIAMETER_MM = 65.0
WHEEL_CIRCUMFERENCE_MM = math.pi * WHEEL_DIAMETER_MM

# 语义方向键 -> (左轮, 右轮) 的转向符号（与固件/实车定版一致）
MOTION_SIGNS = {
    "w": (1, 1),
    "s": (-1, -1),
    "a": (-1, 1),   # 左转：左轮反转、右轮正转
    "d": (1, -1),
}

PATH_MIN_STEP_MM = 12.0
PATH_MAX_POINTS = 1500
ROUTE_START_JITTER_MM = 50.0
ROUTE_CROSS_TRACK_DEADBAND_MM = 15.0


def wrap_angle(angle):
    """把角度折到 (-pi, pi]。"""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    return angle


class Odometry:
    """按指令速度积分推算位姿。速度单位 mm/s，距离 mm，角度 rad。"""

    def __init__(
        self,
        track_mm=120.0,
        max_speed_mm_per_s=250.0,
        left_scale=1.0,
        right_scale=1.0,
        left_deadzone=0.0,
        right_deadzone=0.0,
    ):
        self.track_mm = float(track_mm)
        self.max_speed_mm_per_s = float(max_speed_mm_per_s)
        self.left_scale = float(left_scale)
        self.right_scale = float(right_scale)
        self.left_deadzone = float(left_deadzone)
        self.right_deadzone = float(right_deadzone)
        self.x = 0.0
        self.y = 0.0
        self.theta = 0.0
        self.path = [(0.0, 0.0)]

    def configure(
        self,
        track_mm=None,
        max_speed_mm_per_s=None,
        left_scale=None,
        right_scale=None,
        left_deadzone=None,
        right_deadzone=None,
    ):
        if track_mm is not None and track_mm > 0:
            self.track_mm = float(track_mm)
        if max_speed_mm_per_s is not None and max_speed_mm_per_s > 0:
            self.max_speed_mm_per_s = float(max_speed_mm_per_s)
        if left_scale is not None and left_scale > 0:
            self.left_scale = float(left_scale)
        if right_scale is not None and right_scale > 0:
            self.right_scale = float(right_scale)
        if left_deadzone is not None and 0 <= left_deadzone < 100:
            self.left_deadzone = float(left_deadzone)
        if right_deadzone is not None and 0 <= right_deadzone < 100:
            self.right_deadzone = float(right_deadzone)

    def reset(self, x=0.0, y=0.0, theta=0.0):
        self.x = float(x)
        self.y = float(y)
        self.theta = wrap_angle(float(theta))
        self.path = [(self.x, self.y)]

    def wheel_speeds(self, key, duty_left, duty_right):
        """方向键 + 左右占空比 -> (左轮速度, 右轮速度) mm/s；无指令返回 0。"""
        signs = MOTION_SIGNS.get(key)
        if signs is None:
            return 0.0, 0.0
        return (
            signs[0] * self._duty_speed(duty_left, self.left_scale, self.left_deadzone),
            signs[1] * self._duty_speed(duty_right, self.right_scale, self.right_deadzone),
        )

    def _duty_speed(self, duty, wheel_scale, deadzone):
        duty = max(0.0, min(100.0, float(duty)))
        if duty <= deadzone:
            return 0.0
        # 把死区后的有效占空比重新归一化，默认死区为 0 时兼容旧模型。
        effective = (duty - deadzone) / (100.0 - deadzone)
        return effective * self.max_speed_mm_per_s * wheel_scale

    def integrate(self, v_left, v_right, dt):
        """差速驱动积分一步（中点法，转弯时路径更平滑）。"""
        if dt <= 0:
            return
        v = 0.5 * (v_left + v_right)
        omega = (v_right - v_left) / self.track_mm if self.track_mm > 0 else 0.0
        heading_mid = self.theta + 0.5 * omega * dt
        self.x += v * math.cos(heading_mid) * dt
        self.y += v * math.sin(heading_mid) * dt
        self.theta = wrap_angle(self.theta + omega * dt)
        last = self.path[-1]
        if math.hypot(self.x - last[0], self.y - last[1]) >= PATH_MIN_STEP_MM:
            self.path.append((round(self.x, 1), round(self.y, 1)))
            if len(self.path) > PATH_MAX_POINTS:
                del self.path[: len(self.path) - PATH_MAX_POINTS]

    def snapshot(self):
        return {
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "theta": round(self.theta, 3),
            "theta_deg": round(math.degrees(self.theta), 1),
            "path": self.path,
            "cfg": {
                "wheel_diameter_mm": WHEEL_DIAMETER_MM,
                "wheel_circumference_mm": round(WHEEL_CIRCUMFERENCE_MM, 1),
                "track_mm": round(self.track_mm, 1),
                "max_speed_mm_per_s": round(self.max_speed_mm_per_s, 1),
                "left_scale": round(self.left_scale, 4),
                "right_scale": round(self.right_scale, 4),
                "left_deadzone": round(self.left_deadzone, 2),
                "right_deadzone": round(self.right_deadzone, 2),
            },
        }


def route_command(dx, dy, theta, pivot_deg=35.0, base_speed=100, min_inner=58):
    """朝目标点行驶的转向决策（纯函数）。

    - 航向误差大于 ``pivot_deg``：原地转（key a/d），到位后由调用方继续重算；
    - 否则：前进 + 差速（哪边需要转就慢哪边），速差随误差比例加大。

    返回 ``{"key", "pivot", "left", "right"}``；``key`` 为建议方向键，
    差速档的 left/right 为占空比（pivot 档恒 100/100）。
    """
    err = wrap_angle(math.atan2(dy, dx) - theta)
    if abs(err) >= math.radians(pivot_deg):
        return {
            "key": "a" if err > 0 else "d",
            "pivot": True,
            "left": 100,
            "right": 100,
        }
    inner = int(round(base_speed - (base_speed - min_inner) * abs(err) / math.radians(pivot_deg)))
    inner = max(min_inner, min(base_speed, inner))
    if err > 0:   # 需要逆时针（左转）：左轮为内侧
        return {"key": "w", "pivot": False, "left": inner, "right": base_speed}
    return {"key": "w", "pivot": False, "left": base_speed, "right": inner}


def polyline_command(
    x,
    y,
    theta,
    segment_start,
    segment_end,
    pivot_deg=10.0,
    pivot_duty=45,
    base_speed=100,
    min_inner=70,
    cross_track_gain=0.8,
    lookahead_mm=100.0,
    allow_arc=False,
    turn_mode="auto",
):
    """沿当前规划线段行驶，返回与 route_command 相同的动作字典。

    allow_arc=False 时严格沿线段航向行驶，直线段只允许左右轮对称驱动；
    allow_arc=True 时瞄准线段前方的 lookahead 点，用纯追踪方式把连续曲线
    变成平滑差速弧线。转向策略支持 pivot（两轮反向）、arc（两轮同向差速）、
    one_wheel（外侧单轮高功率）和 auto。该函数只使用估计位姿，不能代替编码器、
    IMU 或视觉反馈。
    """
    sx, sy = segment_start
    ex, ey = segment_end
    seg_x, seg_y = ex - sx, ey - sy
    length = math.hypot(seg_x, seg_y)
    if length <= 1e-9:
        return {"key": None, "pivot": False, "left": 0, "right": 0}

    ux, uy = seg_x / length, seg_y / length
    # 叉积为正表示车在规划线左侧，此时应向右修正航向。
    cross_track = ux * (y - sy) - uy * (x - sx)
    segment_heading = math.atan2(seg_y, seg_x)
    desired_heading = segment_heading
    along = (x - sx) * ux + (y - sy) * uy
    if allow_arc:
        target_along = min(length, max(0.0, along) + max(1.0, lookahead_mm))
        target_x = sx + target_along * ux
        target_y = sy + target_along * uy
        desired_heading = math.atan2(target_y - y, target_x - x)
    elif along >= length - max(1.0, lookahead_mm):
        # 直线主体保持等速；接近有限端点时瞄准端点，避免横向误差让车辆越过路点。
        desired_heading = math.atan2(ey - y, ex - x)
    elif abs(cross_track) > ROUTE_CROSS_TRACK_DEADBAND_MM:
        # 只有实际偏离直线时才纠偏；在线上行驶时不主动把直线变成弧线。
        correction = math.atan2(
            -cross_track_gain * cross_track,
            max(1.0, lookahead_mm),
        )
        desired_heading = segment_heading + correction
    err = wrap_angle(desired_heading - theta)
    limit = math.radians(max(1.0, float(pivot_deg)))
    if abs(err) >= limit:
        selected_mode = turn_mode
        if selected_mode == "auto":
            error_deg = abs(math.degrees(err))
            if error_deg >= 45.0:
                selected_mode = "pivot"
            elif allow_arc and error_deg < 25.0:
                selected_mode = "arc"
            else:
                selected_mode = "one_wheel"
        if selected_mode == "one_wheel":
            return {
                "key": "w",
                "pivot": False,
                "left": 0 if err > 0 else int(base_speed),
                "right": int(base_speed) if err > 0 else 0,
                "turn_mode": "one_wheel",
                "heading_error_deg": math.degrees(err),
                "cross_track_mm": cross_track,
            }
        if selected_mode == "arc":
            inner = max(int(min_inner), int(round(base_speed * 0.78)))
            return {
                "key": "w",
                "pivot": False,
                "left": inner if err > 0 else int(base_speed),
                "right": int(base_speed) if err > 0 else inner,
                "turn_mode": "arc",
                "heading_error_deg": math.degrees(err),
                "cross_track_mm": cross_track,
            }
        return {
            "key": "a" if err > 0 else "d",
            "pivot": True,
            "left": int(pivot_duty),
            "right": int(pivot_duty),
            "turn_mode": "pivot",
            "heading_error_deg": math.degrees(err),
            "cross_track_mm": cross_track,
        }

    if not allow_arc and abs(cross_track) <= ROUTE_CROSS_TRACK_DEADBAND_MM and along < length - max(1.0, lookahead_mm):
        return {
            "key": "w",
            "pivot": False,
            "left": int(base_speed),
            "right": int(base_speed),
            "turn_mode": "straight",
            "heading_error_deg": math.degrees(err),
            "cross_track_mm": cross_track,
        }

    inner = int(round(base_speed - (base_speed - min_inner) * abs(err) / limit))
    inner = max(min_inner, min(base_speed, inner))
    if err > 0:
        left, right = inner, base_speed
    else:
        left, right = base_speed, inner
    return {
        "key": "w",
        "pivot": False,
        "left": left,
        "right": right,
        "turn_mode": "arc",
        "heading_error_deg": math.degrees(err),
        "cross_track_mm": cross_track,
    }


def _point_line_distance(point, start, end):
    """点到线段的距离，供路线去噪使用。"""
    px, py = point
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return math.hypot(px - sx, py - sy)
    t = max(0.0, min(1.0, ((px - sx) * dx + (py - sy) * dy) / length_sq))
    return math.hypot(px - (sx + t * dx), py - (sy + t * dy))


def _rdp(points, tolerance):
    """Ramer-Douglas-Peucker 去掉鼠标采样产生的近共线点。"""
    if len(points) <= 2:
        return list(points)
    start, end = points[0], points[-1]
    split = max(range(1, len(points) - 1), key=lambda i: _point_line_distance(points[i], start, end))
    if _point_line_distance(points[split], start, end) <= tolerance:
        return [start, end]
    return _rdp(points[: split + 1], tolerance)[:-1] + _rdp(points[split:], tolerance)


def _has_sharp_corner(points, corner_deg=45.0):
    limit = math.radians(float(corner_deg))
    for first, corner, last in zip(points, points[1:], points[2:]):
        incoming = math.atan2(corner[1] - first[1], corner[0] - first[0])
        outgoing = math.atan2(last[1] - corner[1], last[0] - corner[0])
        if abs(wrap_angle(outgoing - incoming)) >= limit:
            return True
    return False


def _normalized_route_points(waypoints, origin):
    raw = [tuple(origin)] + [tuple(point) for point in waypoints]
    while len(raw) > 2 and math.hypot(raw[1][0] - raw[0][0], raw[1][1] - raw[0][1]) < ROUTE_START_JITTER_MM:
        del raw[1]
    return raw


def route_has_sharp_corners(waypoints, origin=(0.0, 0.0), simplify_mm=8.0):
    """判断路线是否含有应保留的明确拐角。"""
    raw = _normalized_route_points(waypoints, origin)
    if len(raw) <= 2:
        return False
    return _has_sharp_corner(_rdp(raw, max(0.0, float(simplify_mm))))


def smooth_route(waypoints, origin=(0.0, 0.0), simplify_mm=8.0, max_points=200):
    """把鼠标路线变成控制器真正执行的路线。

    明确拐角的路线保留原折线；连续小转角路线才做 Chaikin 平滑。
    返回值不包含 origin，和 HTTP 路线接口原有格式一致。
    """
    raw = _normalized_route_points(waypoints, origin)
    if len(raw) <= 2:
        return [(round(x, 1), round(y, 1)) for x, y in raw[1:]]
    points = _rdp(raw, max(0.0, float(simplify_mm)))
    if len(points) <= 2:
        return [(round(x, 1), round(y, 1)) for x, y in raw[-1:]]
    if _has_sharp_corner(points):
        return [(round(x, 1), round(y, 1)) for x, y in points[1:]]
    smoothed = [points[0]]
    for first, second in zip(points, points[1:]):
        q = (0.75 * first[0] + 0.25 * second[0], 0.75 * first[1] + 0.25 * second[1])
        r = (0.25 * first[0] + 0.75 * second[0], 0.25 * first[1] + 0.75 * second[1])
        smoothed.extend((q, r))
    smoothed.append(points[-1])
    if len(smoothed) > max_points + 1:
        keep = max_points + 1
        smoothed = [smoothed[round(i * (len(smoothed) - 1) / (keep - 1))] for i in range(keep)]
    return [(round(x, 1), round(y, 1)) for x, y in smoothed[1:]]


if __name__ == "__main__":  # 最小自检：python src/odometry.py
    odo = Odometry(track_mm=100.0, max_speed_mm_per_s=200.0)
    odo.integrate(*odo.wheel_speeds("w", 50, 50), 1.0)
    assert abs(odo.x - 100.0) < 1e-6 and abs(odo.y) < 1e-6 and abs(odo.theta) < 1e-9
    odo.reset()
    odo.integrate(*odo.wheel_speeds("a", 100, 100), 0.1)
    assert abs(odo.x) < 1e-6 and odo.theta > 0.3  # 左转：逆时针、原地不位移
    cmd = route_command(-100.0, 0.0, 0.0)
    assert cmd["pivot"] and cmd["key"] == "a"
    cmd = route_command(100.0, 10.0, 0.0)
    assert not cmd["pivot"] and cmd["left"] < cmd["right"]
    print("odometry self-check OK")
