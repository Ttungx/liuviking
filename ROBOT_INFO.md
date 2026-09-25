# 小车主机连接信息（liuviking-pc）

> 本文件包含明文口令，仅存放于本机项目目录，请勿提交公开仓库或外发。
> 最后更新：2026-09-11（PC 时间）；树莓派无 RTC，系统时间固定 2017-05-22。

## 1. 连接信息汇总

| 项目 | 值 |
|---|---|
| 主机名 | liuviking-pc |
| 硬件 | Raspberry Pi 3B + PWR.A53 扩展板（小R科技 WiFi 视频小车） |
| OS / 内核 | Ubuntu 16.04 LTS (Xenial) / Linux 4.1.19-v7+ armv7l |
| 管理 IP（SSH） | **192.168.88.100**（USB 网卡 `enxb827eba346d3`，PC 侧 IP 192.168.88.2） |
| SSH | 端口 `22`，用户 `liuviking`，密码 `adminadmin` |
| sudo | 密码同 `adminadmin`（无免密 sudo） |
| 控制服务 | TCP **2001**（原厂 `wifirobots.py`） |
| 手机遥控 | 连热点后浏览器打开 **http://192.168.1.1:8082/**（树莓派内置网页遥控，rc.local 自启） |
| 热点 | wlan0 AP：`wifi-robots.com_b827ebf61386`，WPA2 密码 `12345678`，信道 1，网关 192.168.1.1 |
| 视频 | MJPEG **8080 正常出图**（USB 摄像头，2026-09-12 实测验证，PC 与热点侧均可访问） |
| 当前限制 | 电机、云台未连接；电池未开（电压寄存器 0）；系统时间不准 |

## 2. 快速连接 / 验证命令（Windows PowerShell）

```powershell
# 端口连通性
Test-NetConnection 192.168.88.100 -Port 2001

# SSH 登录
ssh liuviking@192.168.88.100

# 项目自带工具（在本项目目录执行）
python tools/probe_robot.py --host 192.168.88.100
python tools/raw_command_test.py --host 192.168.88.100 --command all --duration 0.2
python src/main.py --host 192.168.88.100 --connect
```

树莓派侧检查：

```bash
sudo ss -lntp | grep ':2001'        # 控制服务是否在听
ps aux | grep '[w]ifirobots'        # 原厂进程
iwconfig wlan0 | head -3            # 热点模式（Mode:Master 正常）
pgrep -a hostapd                    # 热点进程
```

## 3. 网络拓扑

| 接口 | 角色 | IP | 说明 |
|---|---|---|---|
| `enxb827eba346d3` | USB 有线网卡 | 192.168.88.100/24 | 与 PC 同网段，SSH/遥控主通道 |
| `wlan0` | WiFi AP | 192.168.1.1/24 | hostapd 热点 + dnsmasq DHCP（192.168.1.2–254） |

- 无默认路由：树莓派本身不能上外网，热点客户端也无 NAT 出口（rc.local 的 iptables 规则仍指向不存在的 `eth0`）。
- 原厂 2001 服务 `listen(1)` 单客户端：遥控时须关闭手机 APP / 官方上位机。

## 4. 控制协议（实机 `wifirobots.py` 源码确认）

帧格式：`FF B0 B1 B2 FF`（5 字节真实 bytes，非 ASCII 字符串）。

| 功能 | 帧（hex） | 备注 |
|---|---|---|
| STOP | `FF 00 00 00 FF` | 松开按键/急停/断开前发送 |
| 前进 W | `FF 00 01 00 FF` | |
| 后退 S | `FF 00 02 00 FF` | |
| 左转 A | `FF 00 03 00 FF` | 实车若左右相反，客户端勾选“左右互换校准” |
| 右转 D | `FF 00 04 00 FF` | |
| 左/右侧速度 | `FF 02 01 XX FF` / `FF 02 02 XX FF` | XX=0x00–0x64（0–100） |
| 舵机 1–8 | `FF 01 NN AA FF` | 角度源码钳制 15–160 |
| 模式 | `FF 13 MM 00 FF` | 01 跟随 / 02 巡线 / 03 红外避障 / 04 超声波避障 / 05 测距 / 06 超声波遥控 / 00 退出 |
| 开/关灯 | `FF 04 00 00 FF` / `FF 04 01 00 FF` | LED0 |
| 读电压 | `FF 05 00 00 FF` | 仅服务端 print（I2C） |
| 心跳 | `FF EF EF EE FF` | API 规定每约 10 秒；当前固件忽略（无害） |

固件无看门狗：客户端不发送后电机保持最后状态，**所有安全停止逻辑必须在客户端**（已实现：松键停、失焦停、退出停、断线停、重连默认停）。

### XRservo 舵机板协议（2026-09-12 反汇编厂商补丁版 smbus.pyc 实测确认）

舵机 MCU 在 I2C1（`/dev/i2c-1`）地址 **0x17**（`SMBus.XR_ADDR=23`），`liuviking` 用户在 `i2c` 组，可直接访问：

| 操作 | I2C 事务（write_byte_data 的 cmd, value） |
|---|---|
| 设舵机 N 号到 A 度 | 写 `(0xFF, N)` 再写 `(A, 0xFF)` 两次事务 |
| 回读舵机 N 号角度 | 读寄存器 `N`（**实测恒为 0xAF，不可信**） |
| 读电池电压 | 读寄存器 `0x12`，按 0.1V 单位（**实测电池未开时为 0**） |
| 左/右轮速计数 | 读寄存器 `0x13` / `0x14` |
| 全部回中 / 存默认角度 | 写 `(0xFF, 0x00)`+`(1,0xFF)` / 写 `(0xFF, 0x11)`+`(1,0xFF)` |

诊断工具：`xr_servo_probe.py`（车上 `/home/liuviking/work/wifirobots/`、仓库 `pi/`），判定表见脚本头注释。

## 5. 原厂服务与关键路径

| 项目 | 路径 / 值 |
|---|---|
| 控制服务源码 | `/home/liuviking/work/wifirobots/wifirobots.py`（581 行，Python 2） |
| 源码备份 | `/home/liuviking/work/wifirobots/wifirobots.py.backup.20170522_071103` |
| 启动脚本 | `/home/liuviking/work/wifirobots/startwifirobot.sh` → `python -u wifirobots.py >> wifirobots.log 2>&1 &`（2026-09-12 修复：stdout 必须落盘，否则固件会"变聋"，见 §7） |
| 启动脚本备份 | `startwifirobot.sh.backup.20170522`（原版：`python wifirobots.py &`） |
| 固件日志 | `/home/liuviking/work/wifirobots/wifirobots.log`（逐帧 `motor_forward` / `motor_stop` / `error4 command!`） |
| 自启动 | `/etc/rc.local` 第 30–31 行 |
| 热点配置 | `/etc/hostapd/hostapd.conf`（SSID 行每次启动由 rc.local 用 MAC 重写） |
| dnsmasq | `/etc/dnsmasq.conf`：`interface=wlan0`，DHCP 192.168.1.2–254 |
| 视频脚本 | `/home/liuviking/work/mjpg-streamer-experimental-xr/start.sh`（8081 后台 + 8080 前台） |
| 手机遥控服务 | `/home/liuviking/work/wifirobots/web_controller.py`（Python2，仅标准库） |
| 遥控启动脚本 | `/home/liuviking/work/wifirobots/start_web_controller.sh` |
| 遥控日志 | `/home/liuviking/work/wifirobots/web_controller.log` |
| Python | `python`=2.7.11+，`python3`=3.5.1+ |

## 6. GPIO（源码确认）

| 用途 | BCM 引脚 |
|---|---|
| ENA / ENB | 13 / 20（PWM 1000Hz） |
| IN1 / IN2 / IN3 / IN4 | 19 / 16 / 21 / 26 |
| LED0 大灯 / LED1 / LED2 | 10 / 9 / 25 |
| 超声波 TRIG / ECHO | 17 / 4 |
| 红外 IR_R / IR_L / IR_M | 18 / 27 / 22 |
| 红外 IRF_R / IRF_L | 23 / 24 |
| 舵机 | I2C `SMBus(1)` + XRservo 扩展板 |

## 7. 本机（PC）对树莓派做过的变更记录

| 变更 | 文件 / 操作 | 回滚方式 |
|---|---|---|
| 源码备份 | `wifirobots.py.backup.20170522_071103` | 直接覆盖回原文件 |
| NM 不管理 wlan0 | 新增 `/etc/NetworkManager/conf.d/unmanaged-wlan0.conf`，并 `nmcli device set wlan0 managed no` | 删除该文件并 `nmcli device set wlan0 managed yes` |
| rc.local 修正 | 启动 hostapd 前加 `systemctl stop wpa_supplicant`；修正重定向写法 | 原始副本在本机 `%TEMP%\opencode\rc.local` |
| SSID 生成修复 | rc.local 改为从 `/sys/class/net/wlan0/address` 取 MAC 生成 `ssid=wifi-robots.com_<MAC>`；修复重启后 SSID 丢 MAC 后缀导致手机连不上的问题 | 恢复原 `ifconfig \| awk '{print $5}'` 写法 |
| 手机页面加开灯/关灯 | `pi/web_controller.py` 新增 `/light?on=0/1` 与页面按钮 | 删除对应路由与按钮 |
| 热点启动 | 手动 `hostapd -B /etc/hostapd/hostapd.conf`（已 AP-ENABLED） | 无需回滚，重启后由 rc.local 拉起 |
| 手机遥控部署 | 上传 `web_controller.py` + `start_web_controller.sh` 到 `/home/liuviking/work/wifirobots/` | 删除这两个文件与日志 |
| rc.local 增加 | 第 32 行 `su liuviking -c 'sh /home/liuviking/work/wifirobots/start_web_controller.sh'` | 删除该行（备份见本机 %TEMP%\opencode\rc.local 历史版本） |
| 固件"变聋"修复（2026-09-12） | `startwifirobot.sh` 改为 `python -u wifirobots.py >> wifirobots.log 2>&1 &`。原方式 stdout 指向已失效的 pts/journal socket，`print` 缓冲刷写异常会终止解码线程：表现为 TCP 能连上、但任何命令都不执行（灯也不亮）。同时重启了固件进程并持续记录 `wifirobots.log` | 恢复 `startwifirobot.sh.backup.20170522` 后重启固件 |
| 手机遥控升级（2026-09-12） | `pi/web_controller.py`：日志加相对时间戳与 `/diag`（记录长按 held 秒数）；长按改用 pointer capture、去掉 pointerleave 误停；新增"取得控制"（`/claim`）：无运动指令时其它页面可抢回控制权 | 覆盖回 `web_controller.py.backup.20170522_101234`（升级前版本） |
| 舵机诊断脚本部署（2026-09-12） | 仓库 `pi/xr_servo_probe.py` 上传到车上 `/home/liuviking/work/wifirobots/xr_servo_probe.py` | 删除车上该文件 |
| 运动看门狗加固（2026-09-12） | `pi/web_controller.py`：按住方向键期间页面每 0.4s 重复 /cmd；服务端新增运动超时（默认 1.2s 无刷新自动 STOP、连接保留）；窗口级 pointerup/pointercancel 兜底；桌面 OS 按键重复作为心跳。已部署并重启 8082（新增 2 个单测，共 79 个全过）。车上备份 `web_controller.py.backup.before_motion_watchdog` | 覆盖回该备份并重启 |
| 全链路安全审查修复（2026-09-12） | ① 桥 `/stop` 急停必达：桥有未决运动且连接断开时先重连再送 STOP（实测 `packets:2` 送达；无未决运动时不抢占 2001，避免误抢 GUI/官方 APP）；② 网页全局 pointerup 兜底收紧为仅匹配触摸按下的指针，键盘遥控不再被鼠标点击误停；③ Windows GUI 新增同款运动刷新看门狗（`MotionRefreshWatchdog`，按住期间靠 Qt 按键自动重复喂狗，1.2s 无刷新急停，防拖动窗口/系统弹窗吞掉 keyup 后跑飞）。83 个单测全过。车上备份 `web_controller.py.backup.before_review_fixes` | 覆盖回该备份并重启；GUI 改动回滚 `src/ui.py`、`src/keyboard_controller.py` |
| 弧线转向（2026-09-12） | 手机页面新增"转向:弧线/原地"切换（默认弧线）：弧线=前进命令 + 差速（内侧 `ARC_INNER=30`%、外侧 `ARC_OUTER=80%`），A=左弧、D=右弧；每次运动前先发速度对；运动超时停车后服务端自动恢复速度 100/100。固件确认：方向命令不碰占空比、速度命令持续生效，故零固件改动。85 个单测全过，已部署（备份 `web_controller.py.backup.before_arc`）。GUI 未加弧线，需要时再说 | 覆盖回该备份并重启 |
| 云台按钮恢复 + 视频状态修复（2026-09-13） | `pi/web_controller.py`：① 恢复被弧线转向部署覆盖丢失的云台 UI（云台上/下/左/右/归位 → `/servo`，按住每 220ms 步进，与 before_arc 备份同款）；② 修复"视频未连接"提示在出图后仍叠加显示的 UI bug：改为三态提示（正在连接视频…/出图即隐藏/视频不可用可重试），用 `naturalWidth>0` 判定第一帧（MJPEG 无限流部分浏览器不触发 load），每次点"视频"重设 src 支持断流重连。运动代码零改动，85 单测全过，已部署并重启 8082（车上备份 `web_controller.py.backup.before_gimbal_restore`） | 覆盖回该备份并重启 |
| 云台映射按实际接线定版（2026-09-13 晚） | 实车四方向按钮实测：两颗舵机插头实际交叉（水平轴在协议 8 号、垂直轴在协议 7 号，两通道均正常）——此前"8 号通道故障"结论修正为当时竖直舵机插头接触不良（GVS 反插/反复插拔遗留），8 号座换水平舵机实测正常；页面按实际接线定版 `GIMBAL_PAN=8`、`GIMBAL_TILT=7`，并按实测翻转上下方向（上=-10、下=+10，左右=+15/-15 已校准）。83 单测全过，已部署重启 8082（车上备份 `web_controller.py.backup.before_final_map`） | 覆盖回备份并重启 |
| 云台通道实测修正（2026-09-13） | `pi/xr_servo_probe.py` 升级为 1-8 号逐通道慢扫（新增 `--only N` 单通道复测，`--save` 保留），并更正"读数通路不可信、以目视为准"的判定说明；实车扫描确认左右舵机应答于协议 7 号，出厂"1 号=左右、2 号=上下"约定对本车不成立。83 单测全过，已部署重启 8082（车上备份 `web_controller.py.backup.before_pan7`、`before_pan_flip`、`before_tilt8`、`xr_servo_probe.py.backup.before_pan7`） | 覆盖回备份并重启 |
| 左右转向互换定版（2026-09-19） | 实车确认电机左右通道接反：协议 `ENA(FF 02 01)` 实为右轮、`ENB(FF 02 02)` 实为左轮，表现"按左转左轮反而转得快"（弧线差速给反）。零固件/接线改动，协议映射层统一互换：① 车上 8082 `pi/web_controller.py`——`MOTION_KEYS` 的 a/d 帧互换 + `SPEED_LEFT/RIGHT` 常量改为 0x02/0x01（语义 left 永远=物理左轮，弧线差速、`/speed` 诊断、看门狗复位 100/100 一处常量全局生效），车上备份 `web_controller.py.backup.before_lrswap_20260919`；② PC 端 8083——`src/config.py` 默认 `swap_left_right=True`（此前配置项存在但默认关，重启即失效）、`web_console.py` 启动按配置恢复互换（`set_swap_default`）、`app.js` 弧线差速随 swap 换边（此前 swap 只换 A/D 命令帧、没换差速侧，正是 8083 上"按左转左轮快"的直接原因）。test_web_bridge 23 例 + test_pc_console 30 例全过 | 车上覆盖回备份并重启；PC 端回滚上述三文件 |
| 运动修复 A+B 档（2026-09-19） | 根因：① 转向无力＝弧线内侧 30% @ 1kHz 硬 PWM 低于齿轮箱静摩擦死区（直行 100% 有劲是旁证）；② 松手不停＝`Motor_Stop` freewheel 滑行＋跨网延迟；③ 切标签页断连＝sid 每次刷新随机＋会话 3s＋后台 setInterval 节流。改动：**车上 8082** `pi/web_controller.py`——`ARC_INNER` 30→58、运动看门狗超时只 STOP 不再复位 100/100（复位会把内侧轮拽回死区）、含上一轮左右互换，备份 `web_controller.py.backup.before_motionfix_20260919`（md5 b7e29ff5）；**车上固件** `wifirobots.py`——`Motor_Stop` freewheel→短刹车（IN1–IN4 全高短接 H 桥下臂；架空拨轮有明显阻尼即生效），备份 `wifirobots.py.backup.before_brake_20260919`（md5 f0f1d47b）；**PC 8083** `src/web/app.js`（`ARC_INNER` 30→58、差速随 swap 换边、SID 改 localStorage 持久化、回前台立即续期）、`src/web_console.py`（`--session-timeout` 3→10s、看门狗只 STOP）、`src/config.py`（`swap_left_right` 默认 True）。⚠ 坑：`~/.xiaor_remote_config.json` 旧值 `swap_left_right:false` 会覆盖默认，须手改为 `true`，否则 8083 重启后 `/api/status` 仍是 `swap:false`（本次已改并实测 `swap:true`）。test_web_bridge 23 + test_pc_console 30 全过；车上两文件与仓库 `pi/` 逐字节一致（md5 核对）。C 档闭环调速（`FF 06` 脉冲计数）不做，需要时再提 | 车上：①覆盖回备份并 `pkill -f web_controller.py` 重启；②覆盖回备份，先 `sudo kill` 旧固件再起（否则 :2001 占用）。PC：回滚三文件并重启 8083 |
| 转向帧回退修正 + 视频代理修复（2026-09-19 晚） | 实车复核发现「按左/右转两侧轮都向前转、无力」：固件日志证明当时测的是**弧线模式**（先发差速对 58/80、再连发 `00 01 00` 前进帧——弧线本就双轮向前，不是故障）；且 09-19「互换定版」把**方向帧也互换了**，这是推导错误：实车 A=物理右轮/B=物理左轮，固件 `TurnLeft`（A 前/B 后）本身=车左转，只需互换**速度通道**；帧互换会让原地转向左右镜像（git 初始版 `A→FF 00 03` 才是对的）。修正：① 车上 8082 `pi/web_controller.py`：a/d 帧改回 03/04（`SPEED_LEFT=0x02` 等速度常量保持不动），车上备份 `web_controller.py.backup.before_unswap_20260919`；② PC 8083：`src/protocol.py` 速度常量统一 left=0x02/right=0x01、`robot_client.command_for_key` 去掉互换、**整套 swap 机制移除**（`/api/swap`、`set_swap_default`、配置项、页面勾选框、`status.swap` 字段）；页面**弧线模式不再持久化**（防 localStorage 残留旧状态），每次进页面默认原地；③ 视频「不可用（摄像头未接或地址不对）」根因＝本机浏览器走系统代理（Clash 7897）解析不了 `car.local`（502）而 IP 直连正常；`/api/status` 新增 `peer`＝已连实车真实 IP，视频 `<img>` 优先用该 IP（车上 8080 实测 200）。测试 112/112 全过（原 4 个 swap 用例改为「方向帧不得互换」+ 速度通道新值）。测转向请确认页面模式＝**原地**：A=左转（左轮反转/右轮正转）、D=右转 | 车上覆盖回 `web_controller.py.backup.before_unswap_20260919` 并重启；PC 端回滚 protocol.py/robot_client.py/web_console.py/config.py/app.js/index.html |
| 连接地址 mDNS 修复（2026-09-19 晚） | 现象：8083 连接报 `连接失败: [Errno 11001] getaddrinfo failed` 或偶发连不上。根因：Windows 对 `car.local` 的 mDNS 解析不稳定（实测有时只回 IPv6 全局地址且 ping 不通，有时直接解析失败），不是小车问题——`10.90.120.89` 的 2001/8080 一直可达。修复：`src/web_console.py` 新增 `_resolve_ipv4()`（`.local` 只取 A 记录）＋主机名→IP 缓存：解析失败自动回退最近一次成功 IP；连接成功后缓存经 `save_config` 写入 `~/.xiaor_remote_config.json` 的 `host_ips`（已种子 `car.local -> 10.90.120.89`）。测试 113/113 全过 | PC 端回滚 web_console.py/config.py；配置文件删 `host_ips` 键 |
| 弧线调参（2026-09-19 晚） | 实车反馈：原地已正常有力；弧线 58/80 速差只有 16%，表现"两侧都转但车不转弯、无力"。外侧 80→**100（满力）**，速差 42 个点、转弯力矩约翻倍；内侧保持 58（30% 实测低于齿轮箱死区，58 是能稳定转的下限）。改动：车上 8082 `pi/web_controller.py`、PC 8083 `app.js` 的 `ARC_OUTER`，页面提示文案同步。车上备份 `web_controller.py.backup.before_arctune_20260919`（58/80 版）；重启后与仓库 md5 `e7b7e101` 核对一致。若仍嫌弧线半径大，下一步把内侧从 58 往 45 回调（不得再低于死区） | 车上覆盖回 `before_arctune_20260919` 并重启，或只改回 `ARC_OUTER = 80`；PC 端回滚 app.js |
| 轨迹检测 + 路线跟随（2026-09-19 晚，新功能） | 仅 PC 8083 实现，车上零改动。① `src/odometry.py`（新）：差速里程计——无编码器，按"指令占空比 × 标定满速"开环积分（轮径 65mm 只换算周长 204.2mm）；坐标 x 右/y 上/θ 逆时针，转弯用中点法；`route_command()` 纯函数：误差 >35° 原地转（a/d），否则前进+差速（内侧随误差比例放慢至 58 下限）。② `src/web_console.py`：跟踪最近占空比与方向键，50ms 里程计线程积分；`/api/odom`（位姿+轨迹+路线状态）、`/api/odom_cfg`、`/api/odom_reset`、`/api/route`（wp=x,y;x,y… 开始路线，起点清零、车头朝 +x）、`/api/route_stop`；路线线程 150ms 决策、看门狗持续续命（线程死则 1.2s 自动 STOP）、卡死/10 分钟超时自停、急停/断开/关页面（会话超时）全都会中止路线；路线行驶中页面方向键被拒绝（先停路线）。③ 前端新增"轨迹与路线"卡片：canvas 画规划路线（橙虚线+路点）/实际轨迹（蓝线）/车位箭头/100mm 网格，可填轮距、满速、容差。测试 135/135（新增 test_odometry 20 例 + 里程计/路线 HTTP 用例）。 ⚠ 开环推算：打滑/负载/电压都会漂，路线越走越偏；升级路径=编码器或摄像头闭环。**首次使用必须标定**：满速＝100% 直行 3 秒测距离÷3；轮距＝左右轮中心距。2026-09-19 晚追加：视频卡片上移到轨迹卡片之前；路径刷新卡顿优化——`/api/odom` 轮询 500ms→**100ms**（10 帧/秒），画布取景从"每帧重算缩放"改为**取景保持、内容越界才重新取景**（`mapView`/`viewCovers`），消除持续缩放抖动；轨迹模块加**启用开关**（localStorage `xr.odomOn` 记忆，关闭时停轮询/禁用控件/画布变暗，若路线在跑会一并停止）；2026-09-19 深夜改：**路线改为画布鼠标直接绘制**——删除坐标输入框，左键按住拖拽=自由绘制（点距≥50mm、上限200点）、右键逐点=连点成线、新增"清空路线"按钮（行驶中清空=停止路线）；页面刷新后若服务端仍有旧路线会自动接管显示 | PC 端回滚 web_console.py/app.js/index.html/style.css 并删 odometry.py/tests/test_odometry.py；本功能不涉及车上文件 |

## 8. 已知问题

1. 供电：电机必须由电池组（BAT 接口，约 7.4V）供电；USB 口 5V 只能维持树莓派/摄像头，电机只会抖动或不动。**USB 供电状态下发运动命令会导致电压跌落、树莓派直接复位（2026-09-12 实测）**——任何电机/舵机测试前先确认电池已开（舵机板电压寄存器 0x12 非零，可用 `xr_servo_probe.py` 查看）。
2. 云台不转（2026-09-13 已解决）：根因多重叠加——① 页面云台 UI 曾被弧线转向部署覆盖丢失（已恢复）；② 出厂"1 号=左右、2 号=上下"通道约定与本车实际接线不符（实际：**水平轴=协议 8 号、垂直轴=协议 7 号**，两颗舵机插头交叉）；③ 竖直舵机插头曾 GVS 反插且接触不良，造成"只嗡嗡不转"的假象——曾误判"舵机报废"和"8 号通道故障"，均被对调实验推翻（该舵机在 7 号正常跟转、8 号座换水平舵机也正常）。最终页面按实际接线映射 `GIMBAL_PAN=8`、`GIMBAL_TILT=7` 并校准四方向，四方向均正常。遗留提示：若云台再现"嗡嗡不转"，先重插竖直舵机插头再排查。舵机板读数通路不可信（电压恒 0、轮速计数不变、角度回读恒 0xAF），诊断一律以目视为准。
3. ~~摄像头未接~~（2026-09-12 更正）：USB 摄像头（038f:6001）已被系统识别（`/dev/video0`），mjpg-streamer 随 rc.local 自启并在 8080 监听；PC 侧 `http://192.168.88.100:8080/?action=snapshot` 与连续流均实测出图（640×480），车上经热点 IP `192.168.1.1:8080` 亦可达。注意：`vcgencmd get_camera` 报 `detected=0` 只代表 CSI 排线口未接官方摄像头，与本 USB 摄像头无关。手机页面"视频"按钮用 `<img>` 放 MJPEG 流，Android Chrome 正常，iOS Safari 不支持 multipart MJPEG（需换播放方案）。
4. 无 RTC：系统时间每次启动重置为 2017-05-22，日志时间戳不可信。
5. 热点客户端无法上外网（iptables 指向不存在的 eth0，且树莓派无默认路由）。
6. 开机 mjpg 启动失败会让 rc.local 以退出码 1 结束（发生在 hostapd 之后，不影响热点）。
7. 板载 LED 行为（固件逻辑）：LED0=大灯（仅开灯指令后亮）、LED1/LED2=方向状态灯（前进两个都亮；后退只 LED2；转向只 LED1；停止都灭）；PWR 为纯硬件电源灯。LED0 已于 2026-09-11 实测验证软件链路可用。
8. 热点无外网时，部分手机会自动断回移动数据：需在手机 WiFi 设置中关闭"自动切换/智能选网"，或手动保持连接。
9. 固件 stdout 不能指向已关闭的终端/journal socket：`print` 缓冲刷写异常会杀死解码线程，表现为"能连上但不执行命令"。现由 `startwifirobot.sh` 重定向到 `wifirobots.log`（见 §7）。
10. 手机页面为单控制端：旧页面/标签页在后台仍会占用控制权，新页面按方向键会被 busy 拒绝（表现为"按了没反应"）。页面已加"取得控制"按钮（无运动指令时可抢回），或停用旧页面约 3 秒后自动接管。
11. 固件对舵机命令无任何日志输出（`SetServoAngle` 不 print），只能靠 `Got data '01',…` 行确认命令到达；舵机板角度回读恒 0xAF 不可信。
12. **开机窗口的电机幽灵动作**（2026-09-12 定位"不受控前进后退"的主因之一）：固件启动即把 ENA/ENB 占空比设为 100%（`ChangeDutyCycle(100)`），靠 IN1–4 拉低保持停止；但树莓派开机到固件运行前（几十秒）这些 GPIO 悬空，L298N 输入漂移会使电机**随机方向自转**。固件日志的 10 次 `WIFIROBOTS START` 对应多次该窗口。缓解：不玩时关电源开关、修好电池插头接触（欠压复位会反复触发）；根治需 IN1–4/EN 加硬件下拉或固件级看门狗（改固件须先备份）。
13. 手机页面丢 pointerup / 页面被杀时，固件保持最后运动状态：旧机制靠 3s 会话看门狗兜底；2026-09-12 起页面按住期间每 0.4s 重复 /cmd、服务端 1.2s 运动超时自动 STOP（见 §7 变更记录），异常运动上限从 3s 收紧到约 1.2s。
14. **视频服务无自愈能力（2026-09-12 实测确认，摄像头"时好时坏"的根因）**：① `start.sh` 里 `-d /dev/video1`（8081）一行是死代码——车上只有 `/dev/video0`（单 USB 摄像头），该实例每次开机必然失败，徒增"mjpg 启动失败"的日志噪音；② mjpg_streamer 单次运行、无守护——实测 2026-09-12 晚 mjpg 在树莓派**不重启**的情况下自行退出（连续 uptime 12 min 而进程消失、8080 失联），之后无人拉起，直到整车重启才恢复；③ rc.local `#!/bin/sh -e` 把热点/固件/网页遥控/mjpg 串成一条链，任何一环失败（如开机竞态下 mjpg 打不开设备，即已知问题 6）后续服务全部不再启动，摄像头排在最末最易受害；④ 开机约 1–2 分钟内 rc.local 尚未跑到 mjpg 行，此时检查会误判"服务未启动"（本日实际踩过）。摄像头硬件与 mjpg 命令本身无问题（手动运行立即出图，当日 4 次验证）。修复方向：start.sh 改守护循环（顺带删 video1 死代码）或在 rc.local 加独立看门狗，改动前备份。
