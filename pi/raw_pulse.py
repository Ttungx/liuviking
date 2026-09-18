# -*- coding: utf-8 -*-
"""直连原厂 2001 服务发一个方向脉冲（绕过网页桥，用于现场诊断）。

用法（树莓派）::

    python raw_pulse.py w 2.0        # 前进 2 秒后自动 STOP
    python raw_pulse.py stop
"""

import socket
import sys
import time

FRAMES = {
    "stop": b"\xff\x00\x00\x00\xff",
    "w": b"\xff\x00\x01\x00\xff",
    "s": b"\xff\x00\x02\x00\xff",
    "a": b"\xff\x00\x03\x00\xff",
    "d": b"\xff\x00\x04\x00\xff",
}


def main(argv):
    key = argv[0].lower() if argv else "w"
    duration = float(argv[1]) if len(argv) > 1 else 2.0
    host = argv[2] if len(argv) > 2 else "127.0.0.1"
    port = int(argv[3]) if len(argv) > 3 else 2001
    if key not in FRAMES:
        print("未知方向: %s" % key)
        return 1
    sock = socket.create_connection((host, port), 2)
    sock.sendall(FRAMES["stop"])
    time.sleep(0.1)
    if key != "stop":
        sock.sendall(FRAMES[key])
        print("已发送 %s，保持 %.1f 秒" % (key, duration))
        sys.stdout.flush()
        time.sleep(duration)
        sock.sendall(FRAMES["stop"])
    print("已发送 STOP 并断开")
    sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
