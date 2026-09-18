#!/bin/sh
# 树莓派：启动手机网页遥控服务（rc.local 调用，以 liuviking 身份运行）
cd /home/liuviking/work/wifirobots || exit 1
nohup python web_controller.py >> web_controller.log 2>&1 &
exit 0
