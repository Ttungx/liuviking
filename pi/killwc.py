# -*- coding: utf-8 -*-
"""安全结束 web_controller.py 进程（按 /proc 命令行精确匹配，不会误杀当前 shell）。"""

import glob
import os
import signal

ME = os.getpid()
TARGET = "web_" + "controller.py"

for path in glob.glob("/proc/[0-9]*/cmdline"):
    try:
        with open(path, "rb") as handle:
            cmd = handle.read().replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    except IOError:
        continue
    if cmd.startswith("python") and TARGET in cmd:
        pid = int(path.split("/")[2])
        if pid != ME:
            try:
                os.kill(pid, signal.SIGTERM)
                print("killed %d: %s" % (pid, cmd))
            except OSError as exc:
                print("kill %d failed: %s" % (pid, exc))
print("done")
