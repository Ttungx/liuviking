# -*- coding: utf-8 -*-
"""树莓派 GPIO 电平只读工具（不改变任何引脚状态）。

直接 mmap /dev/gpiomem 读 BCM2835 GPLEV0 寄存器，绕过 RPi.GPIO，
因此不会与正在运行的原厂 wifirobots.py 冲突。

用法（树莓派，需 root 或 gpio 组）::

    sudo python gpio_read.py

关注引脚（来自 wifirobots.py 源码）：
    ENA=13 ENB=20 IN1=19 IN2=16 IN3=21 IN4=26
    LED0=10(大灯) LED1=9 LED2=25
"""

import mmap
import struct
import sys

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


def main():
    try:
        handle = open("/dev/gpiomem", "r+b")
    except IOError as exc:
        print("无法打开 /dev/gpiomem: %s（需要 root）" % exc)
        return 1
    mem = mmap.mmap(handle.fileno(), 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def level(pin):
        offset = 0x34 + 4 * (pin // 32)
        word = struct.unpack("<I", mem[offset : offset + 4])[0]
        return (word >> (pin % 32)) & 1

    print("引脚          电平")
    for pin, name in WATCH:
        print("GPIO%-2d %-9s %d" % (pin, name, level(pin)))
    mem.close()
    handle.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
