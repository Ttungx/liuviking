# -*- coding: utf-8 -*-
"""XRservo 云台/舵机诊断脚本（树莓派上运行，绕过原厂固件直接驱动舵机板）。

用途：排查"云台/舵机不动"时，确认舵机板供电与各通道舵机本体是否正常。

运行（小车树莓派上，电池打开后再跑）::

    python2 xr_servo_probe.py              # 1-8 号逐通道慢速扫描，盯住哪个通道会动
    python2 xr_servo_probe.py --only 3     # 只扫描 3 号通道（重新接线后复测用）
    python2 xr_servo_probe.py --save       # 结束后把当前角度存为上电默认位置

判定（电压寄存器读数仅供参考：本板读数通路不可靠——轮速计数不随轮动变化、
角度回读恒 0xAF，电压读 0 不代表舵机一定没电，**以目视扫描结果为准**）：

- 扫描时某个通道的舵机会动  =>  该通道有舵机且供电正常。本车 2026-09-13 实测：
    左右云台舵机在 7 号（出厂"1 号=左右、2 号=上下"约定与本车不符）；上下那颗
    GVS 反插纠正后确认在 8 号（嗡嗡叫+微动=有信号但转不动，机械卡滞或舵机故障，空载复测区分）；
- 1-8 号全程纹丝不动        =>  舵机动力电轨没电或舵机输出级故障：
    有万用表则量任一通道红针(V+)对黑针(G)：约 7V => 板输出/信号问题；0V => 动力电没进来；
    无万用表则重点检查舵机板的电源输入线（红黑两根/端子/跳线帽）是否脱落松动；
- 电压寄存器为 0 但扫描会动  =>  读数通路不准，按目视结果走。

注意：扫描时舵机会大幅摆动，别让手指/线材挡住云台。

依赖：smbus（小R定制的 smbus_cffi egg，XRservo 芯片在 I2C1 的 0x17）。
"""
import argparse
import sys
import time

sys.path.insert(0, "/usr/local/lib/python2.7/dist-packages/smbus_cffi-0.5.1-py2.7-linux-armv7l.egg")
import smbus  # noqa: E402


def clamp(angle):
    return max(15, min(160, int(angle)))


def main(argv=None):
    parser = argparse.ArgumentParser(description="XRservo 舵机诊断")
    parser.add_argument("--save", action="store_true", help="结束后把当前角度存为上电默认角度")
    parser.add_argument("--only", type=int, default=None, help="只扫描指定通道 1-8（复测用）")
    args = parser.parse_args(argv)

    bus = smbus.SMBus(1)

    try:
        vol = bus.XiaoRGEEK_ReadVol()
    except Exception as exc:  # noqa: BLE001
        print("读电压失败（舵机板不应答？）: %r" % (exc,))
        return 1
    print("电池电压寄存器: %d  (按 0.1V 解读约 %.1fV)" % (vol, vol / 10.0))
    print("!! 注意：本板读数通路不可靠，电压 0 不代表没电，下面以目视为准。")

    channels = [args.only] if args.only else range(1, 9)
    print("== 逐通道慢速扫描 15 -> 160 -> 90，盯住每一步是哪个舵机在动 ==")
    for servo_id in channels:
        print("-- 正在扫 %d 号通道，请盯住该通道上的舵机 --" % servo_id)
        sys.stdout.flush()
        for angle in (15, 45, 75, 105, 135, 160, 135, 105, 90):
            bus.XiaoRGEEK_SetServo(servo_id, angle)
            time.sleep(0.6)
        print("-- %d 号通道扫描结束。刚才它动了吗？--" % servo_id)
        sys.stdout.flush()
    if args.save:
        time.sleep(0.3)
        bus.XiaoRGEEK_SaveServo()
        print("已把当前角度存为上电默认位置")
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
