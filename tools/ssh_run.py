# -*- coding: utf-8 -*-
"""在小R小车的树莓派上远程执行命令（诊断用）。

用法::

    python tools/ssh_run.py "cat /home/liuviking/work/wifirobots/wifirobots.py | head -80"
    python tools/ssh_run.py --file local.txt --remote /tmp/x.txt   # 上传文件

默认连接参数来自 ROBOT_INFO.md（可用环境变量覆盖）。
"""

import argparse
import os
import sys

import paramiko


def build_arg_parser():
    parser = argparse.ArgumentParser(description="SSH 执行远程命令")
    parser.add_argument("--host", default=os.environ.get("XIAOR_HOST", "192.168.88.100"))
    parser.add_argument("--user", default=os.environ.get("XIAOR_USER", "liuviking"))
    parser.add_argument(
        "--password", default=os.environ.get("XIAOR_PASS", "adminadmin")
    )
    parser.add_argument("command", nargs="?", default="", help="远程命令")
    parser.add_argument("--file", help="上传的本地文件")
    parser.add_argument("--remote", help="上传目标路径")
    parser.add_argument("--sudo", action="store_true", help="用 sudo 执行（密码自动注入）")
    parser.add_argument("--timeout", type=float, default=30.0)
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        args.host,
        username=args.user,
        password=args.password,
        timeout=args.timeout,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        if args.file:
            sftp = client.open_sftp()
            sftp.put(args.file, args.remote)
            sftp.close()
            print("uploaded %s -> %s" % (args.file, args.remote))
            return 0
        if not args.command:
            print("no command given", file=sys.stderr)
            return 2
        if args.sudo:
            args.command = "sudo -S -p '' sh -c " + shell_quote(args.command)
            stdin_pw = args.password + "\n"
        else:
            stdin_pw = ""
        stdin, stdout, stderr = client.exec_command(args.command, timeout=args.timeout)
        if stdin_pw:
            stdin.write(stdin_pw)
            stdin.flush()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace")
        code = stdout.channel.recv_exit_status()
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        if err:
            print("[stderr] " + err.strip(), file=sys.stderr)
        return code
    finally:
        client.close()


def shell_quote(text):
    return "'" + text.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
