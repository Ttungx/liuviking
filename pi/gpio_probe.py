# -*- coding: utf-8 -*-
"""树莓派 GPIO 只读探针（mmap /dev/gpiomem，不改变任何状态）。

输出：
- GPLEV0/GPLEV1 原始字
- 关注引脚的功能（input/output/alt）与电平
- --watch 模式：按间隔连续采样，用于观察命令执行期间的引脚变化

用法::

    sudo python gpio_probe.py                 # 单次快照
    sudo python gpio_probe.py --watch 6       # 连续采样 6 秒（间隔 0.2s）
"""

import mmap
import struct
import sys
import time

# 引脚定义来自 wifirobots.py 源码
WATCH = [
    (9, "LED1"),
    (10, "LED0(大灯)"),
    (25, "LED2"),
    (13, "ENA"),
    (19, "IN1"),
    (16, "IN2"),
    (20, "ENB"),
    (21, "IN3"),
    (26, "IN4"),
]

FSEL_NAMES = {0: "IN", 1: "OUT", 2: "ALT5", 3: "ALT4", 4: "ALT0", 5: "ALT1", 6: "ALT2", 7: "ALT3"}


class Gpio(object):
    def __init__(self):
        handle = open("/dev/gpiomem", "r+b")
        self._mem = mmap.mmap(
            handle.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE
        )

    def word(self, offset):
        return struct.unpack("<I", self._mem[offset : offset + 4])[0]

    def level(self, pin):
        return (self.word(0x34 + 4 * (pin // 32)) >> (pin % 32)) & 1

    def fsel(self, pin):
        word = self.word(4 * (pin // 10))
        return (word >> (3 * (pin % 10))) & 0b111

    def snapshot(self):
        parts = []
        for pin, name in WATCH:
            parts.append("%s=%d%s" % (name, self.level(pin), "" if self.fsel(pin) == 1 else "(%s)" % FSEL_NAMES.get(self.fsel(pin), "?")))
        return "  ".join(parts)

    def raw(self):
        return "GPLEV0=0x%08X GPLEV1=0x%08X" % (self.word(0x34), self.word(0x38))


def main(argv):
    watch = 0.0
    interval = 0.2
    if "--watch" in argv:
        watch = float(argv[argv.index("--watch") + 1])
    if "--interval" in argv:
        interval = float(argv[argv.index("--interval") + 1])

    gpio = Gpio()
    print("== 引脚模式与电平 ==")
    for pin, name in WATCH:
        print(
            "GPIO%-2d %-10s mode=%-4s level=%d"
            % (pin, name, FSEL_NAMES.get(gpio.fsel(pin), "?"), gpio.level(pin))
        )
    print("== 原始寄存器 ==")
    print(gpio.raw())

    if watch > 0:
        print("== 连续采样 %ss ==" % watch)
        deadline = time.time() + watch
        while time.time() < deadline:
            print("%.1f  %s" % (time.time(), gpio.snapshot()))
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
