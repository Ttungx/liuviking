# -*- coding: utf-8 -*-
"""直接驱动 XRservo 舵机板（绕过固件），用于诊断云台。

在树莓派上运行：python2 xr_servo_probe.py
"""
import sys
import time

sys.path.insert(0, "/usr/local/lib/python2.7/dist-packages/smbus_cffi-0.5.1-py2.7-linux-armv7l.egg")
import smbus  # noqa: E402


def try_call(label, fn, *args):
    try:
        value = fn(*args)
        print("%-28s -> %r" % (label, value))
        return value
    except Exception as exc:  # noqa: BLE001
        print("%-28s -> ERROR %s: %s" % (label, type(exc).__name__, exc))
        return None


bus = smbus.SMBus(1)

print("== 状态读取 ==")
try_call("XiaoRGEEK_ReadVol()", bus.XiaoRGEEK_ReadVol)
for n in (1, 2):
    try_call("XiaoRGEEK_Read_ServoAngle(%d)" % n, bus.XiaoRGEEK_Read_ServoAngle, n)

print("== 舵机扫描 1/2 号：60 -> 120 -> 90 ==")
for ang in (60, 120, 90):
    for n in (1, 2):
        try_call("SetServo(%d, %d)" % (n, ang), bus.XiaoRGEEK_SetServo, n, ang)
        time.sleep(0.5)
        try_call("  readback(%d)" % n, bus.XiaoRGEEK_Read_ServoAngle, n)

print("== 舵机 3-8 号探测：各置 90° ==")
for n in range(3, 9):
    try_call("SetServo(%d, 90)" % n, bus.XiaoRGEEK_SetServo, n, 90)
    time.sleep(0.2)
print("done")
