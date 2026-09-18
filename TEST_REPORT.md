# 测试报告（TEST_REPORT）

- 测试日期：2026-09-11（PC 时间）
- 被测对象：Windows 客户端 `src/` + 命令行工具 `tools/` + 实车原厂服务 `wifirobots.py:2001`
- 硬件条件：**电机、云台未连接**；摄像头未接；方向/舵机命令只写 GPIO，无实际运动
- 树莓派地址：`192.168.88.100`（SSH 与 TCP 2001 均可用）

## 1. 自动化单元测试

命令：

```powershell
python -m unittest discover -s tests -v
```

结果：**69 个测试全部通过（OK，约 9s）**。

| 测试文件 | 覆盖内容 | 数量 |
|---|---|---|
| `tests/test_protocol.py` | 停止/前后左右/心跳精确 hex；speed/servo 钳制；灯/模式/电压帧；流解析（粘连、缺帧、超长）；增量解析器（逐字节/分片/去重/垃圾数据/reset） | 22 |
| `tests/test_key_state.py` | 多键优先级、松键回退、重复按下、非方向键忽略、clear | 14 |
| `tests/test_robot_client.py` | 本机假机器人服务器端到端：连接即 STOP、方向字节、断开前 STOP、心跳、速度/舵机、左右互换、坏帧拒绝、网络异常进入 ERROR、连接失败进入 ERROR | 12 |
| `tests/test_video_client.py` | boundary 提取（普通/带引号/缺省）、MJPEG 分片解析（7 字节/1 字节分片）、尾帧不完整忽略、空流 | 8 |
| `tests/test_web_bridge.py` | 桥接：连接即 STOP、方向帧、急停、session 所有权 busy、看门狗超时 STOP 并断开、ping 保活、断线重连；HTTP：页面、/status、/cmd、/stop、423 busy、400 非法键 | 13 |

关键断言示例：

- `FORWARD == ff000100ff`、`STOP == ff000000ff`、`HEARTBEAT == ffefefeeff`
- 速度 250 -> 钳制 100（`ff020164ff`）；-5 -> 0（`ff020200ff`）
- 舵机 5° -> 15°，200° -> 160°
- W+A 按住后松开 A：生效方向回退为 W（不误发 STOP）

## 2. GUI 冒烟测试（离屏模式，无需人工操作）

```powershell
$env:QT_QPA_PLATFORM='offscreen'
python src/main.py --smoke-test 2 --host 192.168.88.100            # 启动/退出正常
python src/main.py --smoke-test 4 --host 192.168.88.100 --connect  # 自动连接实车
```

结果：

- 启动、界面构建、信号桥接、关闭流程均无异常，退出码 0；
- 自动连接实车成功：日志记录 `connected (192.168.88.100:2001)` → 发送 `FF 00 00 00 FF`（连接后默认 STOP）→ 退出时急停 + 断开前 STOP → `disconnected`。

## 3. 实车连通性探测（`tools/probe_robot.py`）

```powershell
python tools/probe_robot.py --host 192.168.88.100 --heartbeat-seconds 12 --video
```

| 项目 | 结果 |
|---|---|
| TCP 2001 | ✅ 连接成功（已发送 STOP） |
| 心跳 | ✅ 12 秒内发送 3 帧（初始 STOP + 1 次心跳 + 结束 STOP） |
| MJPEG :8080 / :8081 | ❌ 无监听（符合摄像头未接、mjpg-streamer 未运行现状） |

## 4. 实车四方向协议验证（`tools/raw_command_test.py`）

```powershell
python tools/raw_command_test.py --host 192.168.88.100 --command all --duration 0.2 --verbose
```

实际发送序列（每条均有日志与 hex）：

| 命令 | 帧 | 到时自动 STOP |
|---|---|---|
| stop | `FF 00 00 00 FF` | — |
| forward | `FF 00 01 00 FF` | ✅ `FF 00 00 00 FF` |
| backward | `FF 00 02 00 FF` | ✅ `FF 00 00 00 FF` |
| left | `FF 00 03 00 FF` | ✅ `FF 00 00 00 FF` |
| right | `FF 00 04 00 FF` | ✅ `FF 00 00 00 FF` |

结束：断开前 STOP → 关闭连接，退出码 0。

测试后实车状态复核：

- `wifirobots.py`（PID 1565）仍在运行，`*:2001` 正常监听；
- 无残留客户端连接；
- hostapd 正常，wlan0 `Mode:Master`。

## 5. 假机器人端到端联调（Phase 4 新增）

`tools/fake_robot.py` 在本机模拟原厂 2001 协议 + MJPEG（内嵌 1x1 JPEG）：

```powershell
python tools/fake_robot.py
python src/main.py --smoke-test 6 --host 127.0.0.1 --connect --video-url "http://127.0.0.1:8080/?action=stream"
```

结果（离屏 GUI）：

| 项目 | 结果 |
|---|---|
| 控制连接 | ✅ 假机器人记录 `客户端连接 127.0.0.1` |
| 连接即 STOP | ✅ `FF 00 00 00 FF 运动 STOP` |
| 退出急停 + 断开 STOP | ✅ 退出时再收到两条 STOP，随后正常断开 |
| 视频 | ✅ `视频流已连接` → `视频首帧已接收（160 字节，1x1）` |
| 视频延迟 | ✅ 改用 `read1` 后首帧 0.2s（改前 3.4s，原因：`read(4096)` 等满缓冲区） |
| 线程隔离 | ✅ 视频不可用时状态仅显示“视频不可用”，遥控/急停不受影响 |

## 6. Windows exe 打包验证

```powershell
build_exe.bat        # PyInstaller 6.22.2
```

- 产物：`dist/xiaor-remote/xiaor-remote.exe`（one-folder，共约 118 MB）
- 冒烟验证：`xiaor-remote.exe --smoke-test 6 --host 127.0.0.1 --connect --video-url ...`
  → 退出码 0；假机器人记录到初始 STOP、退出急停 STOP、断开 STOP 与正常断开。
- 中间产物 `build/` 已清理；`xiaor-remote.spec` 保留用于复现构建。

## 7. 手机网页遥控（树莓派）部署验证

部署：`pi/web_controller.py` + `pi/start_web_controller.sh` → `/home/liuviking/work/wifirobots/`，
以 `su liuviking` 身份由 rc.local 自启（Python 2.7 实测）。

| 验证项 | 结果 |
|---|---|
| 服务进程 / 监听 | ✅ `python web_controller.py`（PID 3474/3593），`*:8082` LISTEN |
| 页面 | ✅ `GET /` 返回 6443 字节，含标题、WASD 按钮、STOP 按钮 |
| 状态接口 | ✅ `GET /status` → `{"ok":true,"robot":"disconnected",...}`（初始） |
| 命令接口 | ✅ `GET /cmd?k=w&sid=test` → `last:"w"`，桥日志 `发送 FF 00 00 00 FF`（连接即停）+ `发送 FF 00 01 00 FF` |
| 看门狗 | ✅ 5 秒后再查状态：`packets:3`、`robot:"disconnected"`、`last:null`（超时自动 STOP 并释放 2001） |
| 控制权恢复 | ✅ 看门狗断开后原厂 2001 监听正常且无残留连接 |
| 会话互斥 | ✅ 单元/HTTP 集成测试覆盖（第二会话 423 busy） |
| rc.local 自启 | ✅ 以 root 执行 `su liuviking -c 'sh start_web_controller.sh'` 实测拉起成功，自启行在第 32 行 |
| 客户端断连 | ✅ 修复了浏览器断连时 BaseHTTPServer 打印 traceback 的问题（`handle` 捕获 socket.error） |

手机侧操作：连热点 `wifi-robots.com_b827ebf61386`（密码 `12345678`）→ 浏览器打开
`http://192.168.1.1:8082/`。当前电机/摄像头未接，操作只会在日志中体现，无实际动作与画面。

### 7.1 实机功能验证（手机 + 板载 LED 反馈，2026-09-11）

摄像头接入后（该次启动 mjpg-streamer 成功监听 8080），用板载 LED 作为执行反馈：

| 验证项 | 结果 |
|---|---|
| LED0 大灯 | ✅ 手机"开灯/关灯"按钮实测亮/灭，证明 `FF 04 00/01 00 FF` 被原厂程序执行 |
| LED1/LED2 方向灯 | ✅ 按住 W 两灯亮，其它方向键按固件规律亮对应灯 |
| 视频 | ✅ 手机可直接观看 MJPEG 画面（8080） |
| 电机动作 | ⏸ 未动作 —— LED 写入与原厂 `Motor_Forward/Backward/Turn*` 中的电机引脚写入在同一函数内（ENA/ENB/IN1-4 先写、LED1/2 后写），说明 GPIO 控制信号已正确输出；待电机/电池接线完成后复验 |

### 7.2 热点 SSID 故障与修复（2026-09-11）

- 现象：重启后手机无法连接/页面打不开。
- 根因：rc.local 用 `ifconfig | awk '{print $5}'` 提取 wlan0 MAC 失败，热点名变成 `wifi-robots.com_`（无 MAC 后缀），手机保存的网络名不匹配。
- 修复：SSID 恢复为 `wifi-robots.com_b827ebf61386`；rc.local 改为从 `/sys/class/net/wlan0/address` 取 MAC，并用 `[ -n "$MAC" ]` 保护，避免再次丢后缀。

## 8. 服务端解码的观测限制（如实记录）

> 2026-09-12 更新：已解决。固件 stdout 已重定向到 `wifirobots.log`，可逐帧观察解码与电机函数调用；同时确认了"stdout 失效会让解码线程死亡、固件变聋"的故障模式，见 §9.3。

- `wifirobots.py` 的控制台输出指向已删除的伪终端（`/dev/pts/1 (deleted)`），syslog 中无 `motor_*` 解码日志，无法从服务端日志逐帧确认。
- RPi.GPIO 未通过 sysfs 导出引脚（`/sys/class/gpio` 下无 `gpio10`），无法用 GPIO 电平间接验证“灯/电机”状态。
- 因此本轮服务端验证的结论边界是：**原厂服务接受了连接与全部帧、未断开、测试后保持监听**；帧内容正确性由“实机源码逐字节对齐 + 本机假服务器端到端测试”保证。
- 待电机/云台接上后，按 `README.md`“实车启用前检查清单”做架空低速方向复验。

## 9. 2026-09-12 实车运动复验与根因定位

### 9.1 直连 TCP 验证（绕过手机页面）

- `python tools/raw_command_test.py --command forward --duration 3`：固件日志逐条记录 `motor forward` → `motor_stop`，实车方向指示灯按规律亮灭 → 电脑→TCP 2001→固件→GPIO 链路完全正常。
- 但轮子只抖动、电机嗡鸣、车不前进 → 电机功率不足，与软件无关。现场供电说明：电池已充满，但整车接的是 USB 口 5V（USB 只能带逻辑电路，带不动电机）。

### 9.2 手机页面长按定位（新增时间戳 + `/diag` 插桩）

- 旧版页面：`pointerleave` 也触发停止，且无长按耗时记录。
- 新版页面（`pi/web_controller.py`）：pointer capture + 去掉 `pointerleave` + `-webkit-touch-callout:none`；日志记录 `/diag ev=up|cancel held=…`。
- 实测（华为鸿蒙浏览器）：按住左转 **7.04 秒**，固件完整执行 7 秒 `motor_turnleft`，**长按链路正常**。
- 真正的"点击没反应"原因：同一时刻存在多个页面会话时，旧页面在后台以 1Hz ping 持续占用控制权，新页面方向键全部被 busy 拒绝（日志中只有 `/stop` 和 `/diag`，没有 `/cmd`）。已新增 `/claim` 与页面"取得控制"按钮（无运动指令时可抢回控制权），旧页面停止活动约 3 秒后也会自动让出。

### 9.3 固件"变聋"问题（树莓派侧，已修复）

- 现象：固件能接受 TCP 连接，但命令不执行（连灯都不亮）。
- 根因：`startwifirobot.sh` 原为 `python wifirobots.py &`，stdout 指向已失效的 pts/journal socket；固件收到每条命令都会 `print`，缓冲刷写异常会终止解码线程，之后收到任何命令都不执行。
- 修复：改为 `python -u wifirobots.py >> wifirobots.log 2>&1 &` 并重启固件；原脚本备份为 `startwifirobot.sh.backup.20170522`。修复后所有命令均能在 `wifirobots.log` 中看到逐帧调用。

### 9.4 结论

- 软件链路（Windows 客户端 / 树莓派网页 / TCP 协议 / 原厂固件）已全部验证正常。
- 未通过项只剩电机实际转动：需要用电池组（约 7.4V，接扩展板电池接口并打开开关）供电，USB 5V 仅能维持树莓派与摄像头。

## 10. 云台/舵机板专项诊断（2026-09-12）

现象：小车可正常移动，但手机页面"云台上/下/左/右/归位"按钮按了摄像头云台不动。

诊断方法：SSH 上车，反汇编厂商定制 `smbus.pyc`（`smbus_cffi-0.5.1` egg）恢复 XRservo 协议，逐层实测。

| 层级 | 验证方式 | 结果 |
|---|---|---|
| 手机页面 → web_controller | `web_controller.log` 中 `/servo` 请求与 `发送 FF 01 NN AA FF` 帧 | ✅ 109 次请求全部正确发帧（如舵机2 `FF 01 02 69 FF`） |
| web_controller → 固件 | 固件日志逐帧解码记录 | ✅ 109 帧 `Got data '01', '02', '69'` 等与发送角度一一对应，`SetServoAngle` 执行且无异常 |
| 固件 → 舵机板（I2C） | 直接调用 `XiaoRGEEK_SetServo(1..8, …)` 绕过固件驱动 | ✅ I2C1 0x17 芯片应答，全部写入无 IOError（ACK） |
| 舵机板 MCU 状态 | 寄存器扫描 + 电压/计数寄存器 | ✅ 寄存器映射自洽（0x12 电压、0x13/0x14 轮速计数为活数据读 0；其余静态 0xAF），MCU 固件存活 |
| 舵机板 → 舵机本体 | 绕过固件直接扫描 1/2 号 15→160→90、3-8 号回中 | ❌ 云台无动作（期间电池电压寄存器读 0：**电池未开**） |

结论：

1. **软件链路（手机页面 → 固件 → I2C 舵机板）完整正常，无需改代码**；云台不动是舵机板之后的物理问题：舵机未插、插错通道或舵机电源轨无电。
2. 诊断期间实测：电池电压寄存器为 0 时给电机发 1.5s 前进脉冲，整车电压跌落复位（rc.local 自动恢复固件与网页服务）。**再次证实 USB 5V 供电下严禁发运动命令**。
3. 舵机板角度回读恒 0xAF、且 SaveServo 后不变，读回功能不可信，只能靠目视确认舵机动作。
4. 已部署车上诊断工具 `/home/liuviking/work/wifirobots/xr_servo_probe.py`（仓库副本 `pi/xr_servo_probe.py`）：**打开电池后**运行 `python2 xr_servo_probe.py`，扫描时观察云台是否摆动即可定位故障层级（判定表见脚本头注释）。通道约定：1 号=左右、2 号=上下，与页面/GUI 一致。

## 11. 小车"不受控前进后退"排查（2026-09-12）

现象：小车可控正常移动，但偶发不受控地自行前进/后退。

| 检查项 | 结果 |
|---|---|
| 固件自主模式（跟随/巡线/避障） | ✅ 排除——日志中 `Got data '13'` 为 0，从未收到模式命令 |
| 固件解码线程崩溃（"变聋"复发） | ✅ 排除——整份 `wifirobots.log` 无任何 Python Traceback |
| 固件被改动 | ✅ 排除——`wifirobots.py` md5 与 2017 原厂备份一致 |
| 页面丢 pointerup / 页面被杀 | ⚠️ **确认存在风险**——日志中大量 `/cmd` 无对应松开记录（最大 `/stop held=85.96s`）；旧机制仅靠 3s 会话看门狗兜底 |
| 固件启动前 GPIO 悬空 | ⚠️ **确认存在机制**——固件启动即 `ENA/ENB ChangeDutyCycle(100)`、IN1–4 拉低；固件运行前的开机窗口内全部悬空，L298N 输入漂移导致电机随机方向自转（今天固件重启 10 次 = 10 个窗口） |

修复（客户端，已部署车上并重启 8082）：

- 页面按住方向键期间每 **0.4s** 重复 `/cmd` 刷新（触摸与桌面键盘按键重复均为心跳）；
- 服务端新增**运动看门狗**：超过 1.2s 无刷新自动 STOP（连接保留），异常运动上限从 3s 收紧到约 1.2s；
- 窗口级 `pointerup`/`pointercancel` 捕获兜底（元素级事件丢失时仍能停）；
- 新增 2 个单测（`test_motion_timeout_stops_but_stays_connected`、`test_motion_refresh_keeps_moving`），全套 79 个测试通过。

遗留（硬件/固件层，无法纯软件根治）：

- 开机窗口的幽灵动作：缓解 = 不玩时关闭电源开关、修复电池插头接触不良（欠压复位会反复触发）；根治 = IN1–4/EN 加硬件下拉电阻，或改固件（须先备份）；
- 原厂固件无看门狗：任何客户端进程被强杀时电机保持最后状态，只能靠客户端侧机制收敛（见 §9.4）。

### 11.1 全链路安全审查（2026-09-12 第二轮，未复现后的代码排查）

逐文件审查 `pi/web_controller.py`（桥+页面）与 `src/`（GUI 客户端）的全部运动路径，发现并修复 3 个边角漏洞：

| # | 位置 | 漏洞 | 修复 |
|---|---|---|---|
| 1 | 桥 `stop()` | 桥与固件连接断开时急停不重连，STOP 送不到固件（固件锁存电机状态） | `/stop` 无连接时先重连（重连自带 STOP）再急停；实机验证断开状态急停 `packets:2` 成功送达 |
| 2 | 网页全局 pointerup 兜底 | 键盘遥控时 `motionPointer===null` 分支过宽，按住 W 点鼠标会误停一下 | 收紧为仅匹配触摸按下的 `pointerId` |
| 3 | GUI `src/ui.py` | 完全依赖 keyup 停车，拖动窗口/系统弹窗吞掉 keyup 后（且用户关闭失焦急停时）电机一直跑 | 新增 `MotionRefreshWatchdog`（`src/keyboard_controller.py` 纯逻辑）：按住期间靠 Qt 按键自动重复喂狗，1.2s 无刷新自动急停；与网页版同款防线 |

审查确认无问题的部分：按键状态机（最后按下优先/松开回退）、重连后清键禁止恢复运动、ERROR 状态拒绝发送、断线/退出急停、心跳线程、协议帧字节映射（单测覆盖）、桥的会话互斥与看门狗时序、页面的多键优先级与 busy 流程。

单测：新增 4 个看门狗用例，全套 83 个通过。部署：网页桥已更新上车（备份 `web_controller.py.backup.before_review_fixes`）；GUI 改动随仓库分发，下次打包 exe 生效。

### 11.2 弧线转向（2026-09-12，"转向同时前进"需求）

固件源码确认：`Motor_TurnLeft/Right` = 一侧正转一侧反转（原地转）；`Motor_Forward` 只设方向不碰占空比；`ENA/ENB_Speed`（`FF 02 01/02 XX FF`）持续生效 → 差速转向可纯客户端实现。

实现（手机页面，已部署）：

- 新增"转向:弧线/原地"切换按钮，默认弧线；切换时先停车；
- 弧线模式 A/D：先发内侧 30%、外侧 80% 速度帧，再发前进帧（promise 链保证顺序）；W/S 及原地模式先恢复 100/100；
- 按住期间 0.4s 心跳重复刷新（速度帧+前进帧）；
- 服务端运动超时停车时附带恢复速度 100/100，页面被杀留下的不对称速度自愈；
- 桥 `/stop` 收紧为"有未决运动才重连"，避免后台页面的 /stop 抢占 GUI/官方 APP 的 2001。

验证：85 个单测全过（新增超时附速度复位、无未决运动不连接、锁存运动重连急停 3 个用例）；实机速度端到端 `packets` 递增正常；实车弧线动作由用户目视复验。

## 12. 结论

| 验收项（交接文档） | 状态 |
|---|---|
| Windows 双击/命令启动 | ✅ `dist\xiaor-remote\xiaor-remote.exe` / `run_gui.bat` / `python app.py` |
| 手机浏览器操控 | ✅ 连热点后打开 `http://192.168.1.1:8082/`（含看门狗/急停/单会话互斥，开机自启） |
| IP 可填写/保存 | ✅ `~/.xiaor_remote_config.json` |
| 2001 状态可见 | ✅ 状态灯（未连接/连接中/已连接/错误）+ 连接详情 |
| WASD 四方向正确 | ✅ 字节级 + 实车发送验证（运动待电机接上后复验） |
| 松键快速 STOP | ✅ 多键状态机 + 单元测试 |
| Space 始终 STOP | ✅ 应用级事件过滤器（含输入框聚焦时） |
| 失焦 STOP | ✅ 默认开启，可配置 |
| 网络断开不恢复运动 | ✅ ERROR 状态清空按键，重连先 STOP |
| 关闭前 STOP | ✅ closeEvent best-effort STOP |
| 日志含 packet hex | ✅ 时间/方向/hex/连接状态 |
| 左右速度 | ✅ 协议源码确认 + UI + 单测 |
| 云台舵机 / 车灯 | ✅ UI + 协议单测；云台 2026-09-12 专项诊断：软件链路逐层实测正常（见 §10），剩余为舵机接线/供电问题，车上已部署 `xr_servo_probe.py` 定位 |
| 视频 | ✅ 独立线程 + 分片解析单测 + 假机器人首帧验证（实机摄像头待复验） |
| 打包 exe | ✅ PyInstaller 构建 + 假机器人冒烟验证 |
| 无硬件联调 | ✅ `tools/fake_robot.py` |
| README/依赖/启动方式 | ✅ `README.md` / `requirements.txt` / `ROBOT_INFO.md` |
| TEST_REPORT.md | ✅ 本文件 |
