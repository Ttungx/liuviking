# Phase 0 实机勘查报告（diagnostics）

- 勘查对象：小R科技树莓派 WiFi 视频小车
- SSH 通道：`liuviking@192.168.88.100`（USB 网卡直连网段）
- 勘查方式：SSH 只读命令；除按交接文档要求备份 `wifirobots.py` 外，未改动树莓派上任何原厂文件
- 勘查时间：PC 时间 2026-09-11；树莓派系统时间为 2017-05-22（无 RTC，每次启动重置）

## 1. 系统

| 项目 | 值 |
|---|---|
| 主机名 | liuviking-pc |
| 发行版 | Ubuntu 16.04 LTS (Xenial Xerus) |
| 内核 | Linux 4.1.19-v7+ #858 SMP armv7l（Raspberry Pi 3B） |
| 登录用户 | liuviking (UID 1000) |
| Python | `python` = 2.7.11+；`python3` = 3.5.1+（原厂服务跑在 Python 2） |
| RTC | 无，系统时间固定为 2017-05-22，仅影响日志时间戳 |

## 2. 网络

| 接口 | 角色 | IP | 说明 |
|---|---|---|---|
| `enxb827eba346d3` | USB 有线网卡 | `192.168.88.100/24` | 与开发 PC 同网段，SSH/遥控主通道 |
| `wlan0` | WiFi AP | `192.168.1.1/24` | hostapd 热点 `wifi-robots.com_b827ebf61386`（WPA2/ch1），dnsmasq DHCP 192.168.1.2–254 |

- 无默认路由：树莓派本身不能上外网；热点客户端也没有 NAT 出口（rc.local 的 iptables 规则仍指向已不存在的 `eth0`）。
- 当前 2001 端口无客户端连接；原厂 `listen(1)`，同一时刻只允许一个控制端。

## 3. 原厂控制服务（TCP 2001）

| 项目 | 值 |
|---|---|
| 监听 | `*:2001`（`listen(1)`，单客户端） |
| 进程 | PID 1565，`python wifirobots.py` |
| 解释器 | `/usr/bin/python2.7` |
| 工作目录 | `/home/liuviking/work/wifirobots` |
| 源码 | `/home/liuviking/work/wifirobots/wifirobots.py`（581 行，Python 2，CRLF） |
| 备份 | `wifirobots.py.backup.20170522_071103`（本次勘查时创建，未修改原件） |
| 启动脚本 | `startwifirobot.sh`：`python wifirobots.py &` |
| 自启动 | `/etc/rc.local` 第 30–31 行 `cd /home/liuviking/work/wifirobots/ && sudo sh startwifirobot.sh` |

## 4. 实机协议（逐条从 wifirobots.py 源码确认，非网上资料）

帧格式：`FF B0 B1 B2 FF`；接收端 `recv(1)` 逐字节、以 `FF` 为起止符，收满 3 个中间字节后调用 `Communication_Decode()`。

| 功能 | B0 | B1 | B2 | 实机字节 |
|---|---|---|---|---|
| 停止 | 00 | 00 | 00 | `FF 00 00 00 FF` |
| 前进 | 00 | 01 | 00 | `FF 00 01 00 FF` |
| 后退 | 00 | 02 | 00 | `FF 00 02 00 FF` |
| 左转 | 00 | 03 | 00 | `FF 00 03 00 FF` |
| 右转 | 00 | 04 | 00 | `FF 00 04 00 FF` |
| 左侧速度 | 02 | 01 | 00–64 | `FF 02 01 XX FF`（XX 为十六进制速度 0–100） |
| 右侧速度 | 02 | 02 | 00–64 | `FF 02 02 XX FF` |
| 舵机 1–8 | 01 | 01–08 | 角度 | `FF 01 NN AA FF`（源码钳制 15–160） |
| 模式切换 | 13 | 01–06/00 | 00 | 01 跟随 / 02 巡线 / 03 红外避障 / 04 超声波避障 / 05 测距回传 / 06 超声波遥控避障 / 00 退出 |
| 开灯 | 04 | 00 | 00 | `FF 04 00 00 FF` |
| 关灯 | 04 | 01 | 00 | `FF 04 01 00 FF` |
| 读电压 | 05 | 00 | 00 | `FF 05 00 00 FF`（仅服务端 print，I2C） |
| 心跳 | EF | EF | EE | `FF EF EF EE FF`（API 文档规定；当前固件无该分支，落入 `error4 command!`，无害） |

关键结论：

1. **速度帧可开放**：源码 `ENA_Speed/ENB_Speed` 直接 `ChangeDutyCycle(值)`，合法值 0–100，超过会异常，客户端必须钳制。
2. **固件没有看门狗**：客户端停止发送后，电机会保持最后状态。松键停止、失焦停止、断线停止必须全部由客户端保证。
3. 测距模式（B0=13,B1=05）下服务端会主动回发 `FF 03 00 <cm> FF`，客户端需容错。
4. 固件不校验长度/校验和，也没有 ACK；本客户端按 5 字节帧严格发送。

## 5. 电机/GPIO（源码确认）

| 用途 | 引脚（BCM） |
|---|---|
| ENA / ENB | 13 / 20（PWM 1000Hz，初始占空比 100） |
| IN1 / IN2 | 19 / 16 |
| IN3 / IN4 | 21 / 26 |
| LED0 大灯 / LED1 / LED2 | 10 / 9 / 25 |
| 超声波 TRIG / ECHO | 17 / 4 |
| 红外 IR_R/IR_L/IR_M | 18 / 27 / 22 |
| 红外 IRF_R/IRF_L | 23 / 24 |
| 舵机 | I2C `SMBus(1)` + XRservo 扩展板 |

## 6. 视频 MJPEG

`/home/liuviking/work/mjpg-streamer-experimental-xr/start.sh`：

```sh
./mjpg_streamer -i "./input_uvc.so -d /dev/video1" -o "./output_http.so -p 8081 -w ./www" &
./mjpg_streamer -i "./input_uvc.so -d /dev/video0" -o "./output_http.so -p 8080 -w ./www"
```

- 当前无 mjpg 进程，8080/8081 均未监听（摄像头未接，rc.local 中 `init_VideoIn failed`）。
- 预期 URL：`http://<ip>:8080/?action=stream`，快照 `?action=snapshot`；备用 8081。视频属 Phase 4，端口以实机为准。

## 7. 安全注意事项（本阶段结论）

- 电机与云台当前未连接：方向/舵机命令只会写到 PWR.A53 GPIO，不会产生实际运动；真正上车前必须架空车轮做 0.2s 短脉冲验证。
- 不要改动原厂 `wifirobots.py`；如需服务端看门狗（Phase 2 可选）必须另行备份并说明回滚方式。
- 遥控期间关闭官方 APP/PC 上位机，避免 2001 端口被占用导致连接失败。
