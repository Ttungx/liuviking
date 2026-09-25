"""小R科技 WiFi 小车 TCP 控制协议。

协议来源：实机源码 /home/liuviking/work/wifirobots/wifirobots.py
（见 diagnostics.md 第 4 节），逐条确认，非网络资料推测。

帧格式（固定 5 字节）::

    FF B0 B1 B2 FF

    B0=0x00 运动:  B1 00=停止 01=前进 02=后退 03=左转 04=右转
    B0=0x02 速度:  B1 01=左(ENA) 02=右(ENB), B2=速度 0x00-0x64 (0-100)
    B0=0x01 舵机:  B1=1..8, B2=角度(源码钳制 15-160)
    B0=0x13 模式:  B1 01..06 / 00=退出
    B0=0x04 车灯:  B1 00=开 01=关
    B0=0x05 电压:  B1 00=读取
    B0=0xEF 心跳:  FF EF EF EE FF（API 文档规定，当前固件忽略）

所有函数返回真实的 ``bytes``，不要发送 ASCII 字符串 "FF000100FF"。
"""

from typing import Iterable, List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

FRAME_START = 0xFF
FRAME_END = 0xFF
FRAME_LENGTH = 5

MOTION_STOP = 0x00
MOTION_FORWARD = 0x01
MOTION_BACKWARD = 0x02
MOTION_LEFT = 0x03
MOTION_RIGHT = 0x04

# 2026-09-19 实车校准：只有速度通道接反（FF 02 01/ENA 实为物理右轮、
# FF 02 02/ENB 实为物理左轮）。方向帧 MOTION_LEFT/RIGHT 已与实车一致，不互换。
SPEED_LEFT = 0x02
SPEED_RIGHT = 0x01

SERVO_MIN_ANGLE = 15
SERVO_MAX_ANGLE = 160
SPEED_MIN = 0
SPEED_MAX = 100

MODE_MASK = 0x13
MODE_FOLLOW = 0x01
MODE_TRACK_LINE = 0x02
MODE_IR_AVOID = 0x03
MODE_ULTRASONIC_AVOID = 0x04
MODE_ULTRASONIC_DISTANCE = 0x05
MODE_ULTRASONIC_REMOTE = 0x06
MODE_NORMAL = 0x00


def build_frame(b0: int, b1: int, b2: int) -> bytes:
    """构造一帧 ``FF B0 B1 B2 FF``，各字段只取低 8 位。"""
    return bytes([FRAME_START, b0 & 0xFF, b1 & 0xFF, b2 & 0xFF, FRAME_END])


# ---------------------------------------------------------------------------
# 运动命令
# ---------------------------------------------------------------------------

STOP = build_frame(0x00, MOTION_STOP, 0x00)
FORWARD = build_frame(0x00, MOTION_FORWARD, 0x00)
BACKWARD = build_frame(0x00, MOTION_BACKWARD, 0x00)
LEFT = build_frame(0x00, MOTION_LEFT, 0x00)
RIGHT = build_frame(0x00, MOTION_RIGHT, 0x00)

#: 方向键 -> 数据帧
MOTION_BY_KEY = {
    "w": FORWARD,
    "s": BACKWARD,
    "a": LEFT,
    "d": RIGHT,
}

#: 中文方向名 -> 数据帧（日志/UI 显示用）
MOTION_BY_NAME = {
    "stop": STOP,
    "forward": FORWARD,
    "backward": BACKWARD,
    "left": LEFT,
    "right": RIGHT,
}

DRIVING_KEYS = tuple(MOTION_BY_KEY.keys())

HEARTBEAT = build_frame(0xEF, 0xEF, 0xEE)


# ---------------------------------------------------------------------------
# 速度
# ---------------------------------------------------------------------------


def clamp_speed(percent: int) -> int:
    """把速度钳制到 0-100（固件直接送入 ChangeDutyCycle，越界会抛异常）。"""
    return max(SPEED_MIN, min(SPEED_MAX, int(percent)))


def speed_command(side: int, percent: int) -> bytes:
    """``side`` 为 :data:`SPEED_LEFT` 或 :data:`SPEED_RIGHT`；``percent`` 0-100。"""
    if side not in (SPEED_LEFT, SPEED_RIGHT):
        raise ValueError("speed side must be SPEED_LEFT or SPEED_RIGHT")
    return build_frame(0x02, side, clamp_speed(percent))


# ---------------------------------------------------------------------------
# 舵机
# ---------------------------------------------------------------------------


def clamp_angle(angle: int) -> int:
    """把舵机角度钳制到 15-160（与固件 ``Angle_cal`` 一致）。"""
    return max(SERVO_MIN_ANGLE, min(SERVO_MAX_ANGLE, int(angle)))


def servo_command(servo_num: int, angle: int) -> bytes:
    """舵机号 1-8，角度按固件范围钳制。"""
    if not 1 <= int(servo_num) <= 8:
        raise ValueError("servo number must be 1..8")
    return build_frame(0x01, int(servo_num), clamp_angle(angle))


# ---------------------------------------------------------------------------
# 车灯 / 模式 / 电压
# ---------------------------------------------------------------------------


def light_command(on: bool) -> bytes:
    """开灯 ``FF 04 00 00 FF`` / 关灯 ``FF 04 01 00 FF``。"""
    return build_frame(0x04, 0x00 if on else 0x01, 0x00)


def mode_command(mode: int) -> bytes:
    """模式切换：见 MODE_* 常量。"""
    return build_frame(MODE_MASK, mode & 0xFF, 0x00)


def read_voltage_command() -> bytes:
    return build_frame(0x05, 0x00, 0x00)


# ---------------------------------------------------------------------------
# 显示 / 帧解析辅助
# ---------------------------------------------------------------------------


def packet_hex(data: bytes) -> str:
    """``b'\\xff\\x00\\x01\\x00\\xff'`` -> ``"FF 00 01 00 FF"``。"""
    return " ".join("%02X" % b for b in data)


def packet_hex_compact(data: bytes) -> str:
    """``b'\\xff\\x00\\x01\\x00\\xff'`` -> ``"ff000100ff"``。"""
    return data.hex()


def parse_stream(data: bytes) -> List[bytes]:
    """从字节流中提取所有合法帧，状态机与固件接收逻辑一致。

    规则（见 wifirobots.py 主循环）：``FF`` 为起止符；两个 ``FF`` 之间
    恰好有 3 个字节才算一帧，否则丢弃。用于测试、日志解析与探测工具。
    """
    frames = []
    middle = []
    in_frame = False
    for byte in data:
        if byte == FRAME_START:
            if in_frame and len(middle) == 3:
                frames.append(bytes([FRAME_START, *middle, FRAME_END]))
            middle = []
            in_frame = True
        elif in_frame:
            middle.append(byte)
            if len(middle) > 3:
                in_frame = False
                middle = []
    return frames


def is_valid_frame(frame: bytes) -> bool:
    return (
        isinstance(frame, (bytes, bytearray))
        and len(frame) == FRAME_LENGTH
        and frame[0] == FRAME_START
        and frame[-1] == FRAME_END
    )


class FrameAccumulator:
    """增量帧解析器：接收任意分片的字节流，产出完整帧。

    用于假机器人、探测工具等接收端；语义与固件 ``FF`` 起止符状态机一致。
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> List[bytes]:
        self._buffer.extend(data)
        raw = bytes(self._buffer)
        frames = parse_stream(raw)
        last = raw.rfind(bytes([FRAME_START]))
        self._buffer = bytearray(raw[last:]) if last >= 0 else bytearray()
        return frames

    def reset(self) -> None:
        self._buffer.clear()


def iter_known_frames() -> Iterable[tuple]:
    """(名称, 帧) 列表，供 UI/测试遍历。"""
    for name, frame in MOTION_BY_NAME.items():
        yield name, frame
    yield "heartbeat", HEARTBEAT
