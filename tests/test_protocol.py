"""协议层单元测试：与实机 wifirobots.py 源码逐字节对齐。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src import protocol  # noqa: E402


class MotionFrameTests(unittest.TestCase):
    def test_exact_bytes(self):
        cases = {
            protocol.STOP: "ff000000ff",
            protocol.FORWARD: "ff000100ff",
            protocol.BACKWARD: "ff000200ff",
            protocol.LEFT: "ff000300ff",
            protocol.RIGHT: "ff000400ff",
            protocol.HEARTBEAT: "ffefefeeff",
        }
        for frame, expected in cases.items():
            self.assertEqual(frame.hex(), expected)
            self.assertEqual(len(frame), protocol.FRAME_LENGTH)
            self.assertTrue(protocol.is_valid_frame(frame))

    def test_key_mapping(self):
        self.assertEqual(protocol.MOTION_BY_KEY["w"], protocol.FORWARD)
        self.assertEqual(protocol.MOTION_BY_KEY["s"], protocol.BACKWARD)
        self.assertEqual(protocol.MOTION_BY_KEY["a"], protocol.LEFT)
        self.assertEqual(protocol.MOTION_BY_KEY["d"], protocol.RIGHT)

    def test_build_frame_masks_fields(self):
        self.assertEqual(protocol.build_frame(0x100, 0x1FF, 0x2FF).hex(), "ff00ffffff")

    def test_build_frame(self):
        self.assertEqual(protocol.build_frame(0x00, 0x01, 0x00).hex(), "ff000100ff")


class SpeedTests(unittest.TestCase):
    def test_speed_values(self):
        self.assertEqual(protocol.speed_command(protocol.SPEED_LEFT, 100).hex(), "ff020164ff")
        self.assertEqual(protocol.speed_command(protocol.SPEED_RIGHT, 0).hex(), "ff020200ff")
        self.assertEqual(protocol.speed_command(protocol.SPEED_LEFT, 50).hex(), "ff020132ff")

    def test_speed_clamped_to_0_100(self):
        self.assertEqual(protocol.speed_command(protocol.SPEED_LEFT, 250).hex(), "ff020164ff")
        self.assertEqual(protocol.speed_command(protocol.SPEED_RIGHT, -5).hex(), "ff020200ff")

    def test_invalid_side_raises(self):
        with self.assertRaises(ValueError):
            protocol.speed_command(0x03, 50)


class ServoTests(unittest.TestCase):
    def test_angle_values(self):
        self.assertEqual(protocol.servo_command(1, 90).hex(), "ff01015aff")
        self.assertEqual(protocol.servo_command(8, 15).hex(), "ff01080fff")

    def test_angle_clamped_like_firmware(self):
        self.assertEqual(protocol.servo_command(1, 5), protocol.servo_command(1, 15))
        self.assertEqual(protocol.servo_command(1, 200), protocol.servo_command(1, 160))

    def test_invalid_servo_number(self):
        with self.assertRaises(ValueError):
            protocol.servo_command(0, 90)
        with self.assertRaises(ValueError):
            protocol.servo_command(9, 90)


class AuxCommandTests(unittest.TestCase):
    def test_light(self):
        self.assertEqual(protocol.light_command(True).hex(), "ff040000ff")
        self.assertEqual(protocol.light_command(False).hex(), "ff040100ff")

    def test_modes(self):
        self.assertEqual(protocol.mode_command(protocol.MODE_FOLLOW).hex(), "ff130100ff")
        self.assertEqual(protocol.mode_command(protocol.MODE_NORMAL).hex(), "ff130000ff")

    def test_read_voltage(self):
        self.assertEqual(protocol.read_voltage_command().hex(), "ff050000ff")


class StreamParserTests(unittest.TestCase):
    def test_roundtrip_concatenated_frames(self):
        data = protocol.STOP + protocol.FORWARD + protocol.HEARTBEAT
        self.assertEqual(
            protocol.parse_stream(data),
            [protocol.STOP, protocol.FORWARD, protocol.HEARTBEAT],
        )

    def test_rejects_short_and_long_middle(self):
        self.assertEqual(protocol.parse_stream(b"\xff\x00\x01\xff"), [])
        self.assertEqual(protocol.parse_stream(b"\xff\x00\x01\x02\x03\xff"), [])

    def test_ignores_garbage_between_frames(self):
        data = b"\x12\x34" + protocol.LEFT + b"\x99" + protocol.RIGHT
        self.assertEqual(protocol.parse_stream(data), [protocol.LEFT, protocol.RIGHT])

    def test_incomplete_frame_is_ignored(self):
        self.assertEqual(protocol.parse_stream(protocol.FORWARD[:-1]), [])


class FrameAccumulatorTests(unittest.TestCase):
    def test_split_feeding(self):
        accumulator = protocol.FrameAccumulator()
        data = protocol.FORWARD + protocol.LEFT + protocol.STOP
        frames = []
        for index in range(0, len(data), 2):
            frames.extend(accumulator.feed(data[index : index + 2]))
        self.assertEqual(frames, [protocol.FORWARD, protocol.LEFT, protocol.STOP])

    def test_byte_by_byte(self):
        accumulator = protocol.FrameAccumulator()
        frames = []
        for byte in protocol.BACKWARD + protocol.HEARTBEAT:
            frames.extend(accumulator.feed(bytes([byte])))
        self.assertEqual(frames, [protocol.BACKWARD, protocol.HEARTBEAT])

    def test_no_duplicate_frames(self):
        accumulator = protocol.FrameAccumulator()
        self.assertEqual(accumulator.feed(protocol.FORWARD), [protocol.FORWARD])
        self.assertEqual(accumulator.feed(protocol.FORWARD), [protocol.FORWARD])

    def test_garbage_is_dropped(self):
        accumulator = protocol.FrameAccumulator()
        self.assertEqual(accumulator.feed(b"\x01\x02\x03"), [])
        self.assertEqual(accumulator.feed(protocol.RIGHT), [protocol.RIGHT])

    def test_reset(self):
        accumulator = protocol.FrameAccumulator()
        accumulator.feed(b"\xff\x00\x01")
        accumulator.reset()
        self.assertEqual(accumulator.feed(protocol.STOP), [protocol.STOP])


class DisplayHelperTests(unittest.TestCase):
    def test_packet_hex(self):
        self.assertEqual(protocol.packet_hex(protocol.FORWARD), "FF 00 01 00 FF")
        self.assertEqual(protocol.packet_hex_compact(protocol.HEARTBEAT), "ffefefeeff")

    def test_is_valid_frame(self):
        self.assertTrue(protocol.is_valid_frame(protocol.STOP))
        self.assertFalse(protocol.is_valid_frame(b"\xff\x00"))
        self.assertFalse(protocol.is_valid_frame(b"\x00\x00\x00\x00\x00"))


if __name__ == "__main__":
    unittest.main()
