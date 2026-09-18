# -*- coding: utf-8 -*-
"""GPIO 寄存器直读探针 v2（/dev/mem，只读）。

背景：树莓派 4.1 内核上 /dev/gpiomem 映射行为异常（读到 ASCII "gpio"），
本脚本改用 /dev/mem @ 外设基址（Pi3 = 0x3F200000）只读采样。

用于验证原厂 wifirobots.py 在执行运动命令时是否真正驱动电机/LED 引脚。

用法（树莓派 root）::

    python gpio_scan.py                      # 单次快照
    python gpio_scan.py --watch 8 --interval 0.05
"""

import mmap
import os
import struct
import sys
import time

BASE = 0x3F200000
WATCH = [
    (9, "LED1"), (10, "LED0"), (25, "LED2"),
    (13, "ENA"), (19, "IN1"), (16, "IN2"),
    (20, "ENB"), (21, "IN3"), (26, "IN4"),
]
FSEL_NAMES = {0: "IN", 1: "OUT", 2: "ALT5", 3: "ALT4", 4: "ALT0", 5: "ALT1", 6: "ALT2", 7: "ALT3"}


class Gpio(object):
    def __init__(self, base=BASE):
        fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
        self._mem = mmap.mmap(
            fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ, offset=base
        )

    def word(self, off):
        return struct.unpack("<I", self._mem[off:off + 4])[0]

    def level(self, pin):
        return (self.word(0x34 + 4 * (pin // 32)) >> (pin % 32)) & 1

    def fsel(self, pin):
        return (self.word(4 * (pin // 10)) >> (3 * (pin % 10))) & 0b111

    def line(self):
        out = []
        for pin, name in WATCH:
            out.append("%s=%d/%s" % (name, self.level(pin), FSEL_NAMES.get(self.fsel(pin), "?")))
        return " ".join(out)


def main(argv):
    watch = float(argv[argv.index("--watch") + 1]) if "--watch" in argv else 0.0
    interval = float(argv[argv.index("--interval") + 1]) if "--interval" in argv else 0.1
    base = int(argv[argv.index("--base") + 1], 0) if "--base" in argv else BASE
    gpio = Gpio(base)
    print("base=0x%X" % base)
    print("GPFSEL0-5: " + " ".join("0x%08X" % gpio.word(4 * i) for i in range(6)))
    print("GPLEV0=0x%08X GPLEV1=0x%08X" % (gpio.word(0x34), gpio.word(0x38)))
    print("idle  " + gpio.line())
    if watch > 0:
        t0 = time.time()
        deadline = t0 + watch
        n = 0
        while time.time() < deadline:
            print("%6.2f  %s" % (time.time() - t0, gpio.line()))
            n += 1
            time.sleep(interval)
        print("samples=%d" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
