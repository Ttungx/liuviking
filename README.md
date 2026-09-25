# xiaor-remote — 小R科技树莓派 WiFi 小车 PC 端网页控制台

在 PC 上用浏览器 + WASD 键盘遥控小R科技（XiaoR GEEK）树莓派 WiFi 视频小车。
**复用原厂 `wifirobots.py` 的 2001 控制服务，不改 GPIO、不覆盖原厂固件。**
控制台只用 Python 标准库，无第三方依赖。

```text
浏览器 (src/web，WASD 键盘)
      │ HTTP 127.0.0.1:8083
      ▼
src/web_console.py（本机服务）
      │ 持久 TCP :2001（5 字节帧）
      ▼
树莓派原厂 wifirobots.py（192.168.88.100）
      │ GPIO / PWM
      ▼
PWR.A53 扩展板 → 电机
```

## 当前开发状态（重要）

| 项目 | 状态 |
|---|---|
| 实机勘查（Phase 0） | ✅ 完成，见 `diagnostics.md` |
| 协议验证工具（Phase 1） | ✅ 完成，`tools/raw_command_test.py` |
| WASD 客户端（Phase 2） | ✅ 协议/连接层在 `src/`；PC 端界面 2026-09-19 由 PySide6 桌面 GUI 改为网页控制台（`src/web_console.py` + `src/web/`），Qt 依赖已移除 |
| 安全完善 + 单元测试（Phase 3） | ✅ 完成，`tests/`（本地假服务器端到端验证，含 `tests/test_pc_console.py` 20 例） |
| 速度 / 云台舵机 / 车灯 UI | ✅ 完成（协议已按实机源码实现）；云台 2026-09-12 专项诊断：手机页面→固件→I2C 舵机板逐层实测正常，云台不动为舵机接线/供电问题，见 `pi/xr_servo_probe.py` |
| MJPEG 视频（Phase 4） | ✅ 浏览器直连小车 `:8080/?action=stream`，不再经过 PC 端代理；**2026-09-12 实机验证出图**（USB 摄像头 + 8080 自动启动，当时由 Qt 客户端验证）；`tools/fake_robot.py` 可联调 |
| 手机网页遥控（树莓派侧） | ✅ 已开发并部署，`http://192.168.1.1:8082/`，rc.local 开机自启 |
| 本地假机器人 | ✅ `tools/fake_robot.py`（无硬件联调控制台/工具） |
| Windows exe 打包 | ⚠️ `build_exe.bat` 已改为打包静态前端（`--add-data src\web`），**改完后未重新打包验证**；日常直接用 `python src/main.py` 即可 |
| 实车运动验证 | ⚠️ 软件链路已确认（方向指示灯 + 固件 `wifirobots.log` 逐帧）；2026-09-12 实测直连 TCP 前进 3 秒时电机只在 USB 5V 供电下抖动 —— 必须用电池组（约 7.4V）供电后复验 |
| 服务端看门狗 | ⏸ 未做（固件无看门狗，安全全部由客户端保证；改动固件前必须备份） |

## 目录结构

```text
liuviking/
├─ README.md                 本文件
├─ ROBOT_INFO.md             主机连接信息（IP/口令/协议/变更记录）
├─ requirements.txt          依赖说明（PC 端只用标准库，无需安装）
├─ diagnostics.md            Phase 0 实机勘查报告（系统/进程/协议/视频）
├─ TEST_REPORT.md            测试报告（单元测试 + 实机 + 假机器人联调）
├─ app.py                    顶层入口（python app.py，打包用）
├─ run_gui.bat               双击启动控制台（起本地 HTTP 服务并打开浏览器）
├─ build_exe.bat             双击打包 exe（PyInstaller，--add-data 打入前端静态文件）
├─ dist/xiaor-remote/        旧版 Qt 客户端的打包产物（已过时）
├─ pi/
│  ├─ web_controller.py      车上旧版手机遥控服务（Python2/3 兼容）—— 比车上实际部署的落后，
│  │                         参考请用下面那份，别照它抄参数
│  ├─ web_controller.deployed-20260919.py  车上现役文件备份（云台触控板/pan7-tilt8/归位/限位）
│  └─ start_web_controller.sh 树莓派启动脚本（rc.local 调用）
├─ src/
│  ├─ main.py                程序入口（python src/main.py）
│  ├─ web_console.py         网页控制台服务（HTTP + 运动/会话看门狗 + 日志流）；同一份代码
│  │                         可跑在笔记本或车上，靠 --bind/--port/--robot-host 区分
│  ├─ web/                   前端静态文件（index.html / style.css / app.js）
│  ├─ protocol.py            5 字节协议帧 + 增量解析（与实机源码逐字节对齐）
│  ├─ robot_client.py        线程安全 TCP 客户端（心跳/断线处理/左右校准）
│  ├─ keyboard_controller.py 运动刷新看门狗（服务端复用）+ 多键状态机（浏览器侧同款实现）
│  ├─ video_client.py        MJPEG 读取（Qt 客户端移除后仅 tools/tests 使用）
│  └─ config.py              配置持久化（~/.xiaor_remote_config.json，CLI 默认值）
├─ tools/
│  ├─ raw_command_test.py    单方向短脉冲协议验证（到时自动 STOP）
│  ├─ probe_robot.py         TCP 2001 / 心跳 / MJPEG 探测
│  └─ fake_robot.py          本地假机器人（2001 + MJPEG，无硬件联调）
└─ tests/
   ├─ test_protocol.py       帧字节、速度/舵机钳制、流/增量解析
   ├─ test_key_state.py      多键优先级、松键回退
   ├─ test_robot_client.py   本机假机器人服务器的端到端测试
   ├─ test_pc_console.py     PC 端网页控制台 HTTP -> 协议帧端到端 + 看门狗/空闲释放
   └─ test_video_client.py   MJPEG boundary 提取与分片解析
```

## 快速开始

### 1. 依赖

无。PC 端控制台只用 Python 标准库（需 Python 3.7+）。`tools/` 与 `tests/` 同样零依赖。

### 2. 验收连通性（不动电机）

```powershell
python tools/probe_robot.py --host 192.168.88.100
python tools/probe_robot.py --host 192.168.88.100 --heartbeat-seconds 12 --video
```

### 3. 方向协议短脉冲验证（电机未接时安全）

```powershell
# 每个方向 0.2 秒脉冲，到时自动 STOP
python tools/raw_command_test.py --host 192.168.88.100 --command all --duration 0.2
```

### 4. 启动 PC 端网页控制台

```powershell
python src/main.py                                   # 起服务并自动打开浏览器 http://127.0.0.1:8083/
python src/main.py --robot-host car.local --connect   # 启动即连小车（热点"111"下用 .local）
python src/main.py --port 9000 --no-browser           # 换端口 / 不自动开浏览器
python src/main.py --session-timeout 8                # owner 无请求多久后释放 2001（默认 600s）
```

也可以在资源管理器里双击 `run_gui.bat`。IP/端口/速度/校准项记在浏览器
localStorage 里，下次打开自动回填。

浏览器里：填小车 IP → 点"连接" → 键盘 W/S/A/D 驾驶、Space 急停、Esc 停止并断开。
方向键也可以用鼠标/触摸按住驾驶（按住期间每 0.4s 重发方向帧喂看门狗），中间的 STOP 可点。

### 5. 手机操控（树莓派内置网页遥控，推荐）

树莓派上已部署 `pi/web_controller.py`（端口 8082，rc.local 开机自启）。

1. 手机 WiFi 连接小车热点 `wifi-robots.com_b827ebf61386`，密码 `12345678`（提示“无互联网”属正常）。
2. 手机浏览器打开 `http://192.168.1.1:8082/`。
3. 按住方向键移动，松开／切后台立即停止；红色 STOP 为急停。桌面浏览器也可用 W/A/S/D + 空格。
4. **转向方式**：页面“转向:弧线/原地”按钮切换（默认原地）。弧线= A/D 边走边转（前进命令 + 内侧轮 58%、外侧轮 100% 差速）；原地=两轮反向原地转。W/S 始终直行；切换回直行时会重新下发左右轮速。
5. 摄像头已可用（2026-09-12 实测）：点”视频”看 MJPEG（默认 `:8080/?action=stream`）。Android 浏览器正常；iOS Safari 不支持 MJPEG `<img>`，需用其它端。

安全与并发：

- 会话看门狗：600 秒无页面活动自动 STOP 并断开 2001，把控制权让给官方 APP；运动刷新看门狗 1.2 秒未收到续发命令时只发送 STOP；
- 同一时刻只允许一个浏览器会话控制，其它设备显示“其它设备控制中”；
- 换页/刷新后若方向键无反应（旧页面还在后台占用控制权），点页面上的“取得控制”即可抢回；正在被按住运动时不可抢控；
- 页面断网／切后台／失焦都会立即发 STOP；连接原厂服务后第一时间发 STOP。

树莓派侧管理：

```bash
cd /home/liuviking/work/wifirobots
sh start_web_controller.sh      # 启动
pkill -f web_controller.py      # 停止
tail -f web_controller.log      # 日志
```

### 6. 无硬件联调（推荐先用假机器人验证一切）

```powershell
# 终端 1：本地假机器人（2001 控制服务 + 8080 MJPEG）
python tools/fake_robot.py

# 终端 2：网页控制台连假机器人
python src/main.py --robot-host 127.0.0.1 --connect
```

浏览器里把视频端口保持默认 8080、勾选"启用"，就能看到假机器人的 MJPEG 画面。

假机器人会把收到的每一帧解码打印（`运动 STOP`、`速度 左侧 80%`、`舵机 1 -> 90°` 等），
控制台的"日志"面板也会原样显示每一帧的十六进制，可据此核对发出的内容。

### 7. 界面功能说明

- **连接**：填小车 IP 与端口（默认 2001），点"连接/断开"；顶栏显示状态与"发送 N 帧 · 速率"。
- **运动**：方向键高亮当前生效方向，下方读数显示最近一帧的字节与距今秒数。
- **转向模式**：`原地` = A/D 走固件原生单轮反转原地转；`弧线` = A/D 边走边转（先发内侧轮 30%、
  外侧轮 80%，再发"前进"）。切模式或停车后下一条命令会自动重发轮速，不会残留不对称速度。
- **控制权**：同一时刻只有一个浏览器会话能发指令（服务端按 `sid` 记录 owner）。别的设备
  活跃持有时本页显示"其它设备控制中"并禁用方向输入，点"取得控制"抢占；对方正按住方向键时
  抢不过来。**急停不受所有权限制**，锁定态下 STOP 依然送达。打开页面即自动申请控制权。
- **速度**：左右轮 0–100% 滑块，点"应用速度"一次下发两侧。（协议已按实机源码确认）
- **校准**：勾选"左右互换校准"即时生效，无需重连。
- **云台（无级调节）**：拖动左侧**二维触控板**或"水平/垂直"滑块即可连续调到任意姿态；
  方向键 `↑↓` 调垂直、`←→` 调水平，按住由 60ms 定时器每 tick 走 2°（不依赖系统按键重复，
  所以是平滑连续转动而不是定长一跳）；`水平 −` / `水平 +` / `垂直 −` / `垂直 +` 做 1° 微调。
  **姿态、归位点、行程限位都由服务端权威记录**（`Console.gimbal` / `Console.home` /
  `GIMBAL_LIMITS`），页面只显示返回值，换标签页/刷新不会丢姿态。
  通道映射为 **2026-09-18 相机位移实测修正：水平轴 = 协议 7 号、垂直轴 = 8 号**，
  水平 + 为左转、垂直 − 为上仰；实车可用行程 **pan 0–185 / tilt 78–170**，比通用舵机的
  15–160 宽，因此云台走 `build_frame` 直接下发、不复用 `clamp_angle`。
  接线不同用 `--pan-servo` / `--tilt-servo` 改通道。
- **归位 / 设为归位点**：`归位` 回到服务端记录的归位点，`设为归位点` 把当前姿态记为归位点。
- **其它舵机**：选择舵机号 1–8，拖动角度 15–160°，点"应用"或"复位 90°"（走通用钳制）。
- **车灯**：单个按钮切换开/关（LED0），按钮文字显示当前状态。
- **视频**：填视频端口（默认 8080）与路径（默认 `/?action=stream`），勾选"启用"。
  浏览器直连小车取流，**视频不可用完全不影响遥控与急停**。
- **日志**：服务端 logging 记录按秒增量推给浏览器，按级别着色，可清空。

### 8. 运行单元测试

```powershell
python -m unittest discover -s tests -v
```

### 9. 打包 exe（可选）

```powershell
build_exe.bat      # PyInstaller：产物在 dist\xiaor-remote\
```

前端静态文件用 `--add-data` 一起打包，运行时从 `sys._MEIPASS` 读取。
**注意：改为网页控制台后这条打包路径尚未重新验证**，日常直接 `python src/main.py` 即可。

## 操作与安全设计

| 操作 | 行为 |
|---|---|
| W / S / A / D | 前进 / 后退 / 左转 / 右转（按住持续，松开即停） |
| ↑ / ↓ / ← / → | 云台两轴连续转动（按住时每 60ms 走 2°），已 `preventDefault`，不会滚动页面 |
| 多键同时按 | 最后按下的方向优先；松开后回退到仍按住的方向，全部松开才 STOP |
| 转向模式 = 弧线 | A/D 改为"内侧轮 58% + 外侧轮 100% + 前进"；切模式或停车后下一条命令重发轮速 |
| 单控制端 | 服务端按 `sid` 记 owner，旁人指令返回 `busy` + HTTP 423；`取得控制` 走 `/api/claim` 抢占，对方正按住方向键时拒绝 |
| 急停不受限 | `/api/stop` 任何会话都能发，锁定态下照样送达 |
| 按住期间的持续刷新 | 页面每 0.4s 重复发送当前方向；服务端超过 1.2s 没刷新就自动 STOP（页面被杀/断网/丢事件时电机最多再跑约 1.2s） |
| 急停必达 | 页面 STOP 即使在桥与固件连接已断时也会先重连再送 STOP |
| Space | 紧急停车（焦点在页面上即生效，输入框内除外） |
| Esc | 停止 + 断开连接 |
| 页面失焦 / 切后台 | 立即 STOP（浏览器页面无法像桌面程序那样”失焦后继续安全遥控”，因此不提供关闭开关） |
| 页面被关掉 | owner 不再有 API 请求，超过 `--session-timeout`（默认 600s）后服务端 STOP 并断开 2001，把控制权让给另一台设备或官方 APP |
| Ctrl+C 关闭服务 | best-effort STOP 后断开 |
| 网络异常 | 状态变红”连接错误”，清空按键，拒绝继续发送运动指令 |
| 重新连接 | 连接成功后先发 STOP，绝不恢复上一次运动 |

其它：

- 心跳每约 10 秒发送 `FF EF EF EE FF`（API 文档规定；当前固件忽略该帧，无害）。
- Socket 写操作加锁；连接超时默认 1.5 秒。
- 原厂服务 `listen(1)` 单客户端：遥控前请关闭手机 APP/官方上位机。
- 界面可勾选“左右互换校准”——若实车左右相反，在客户端映射层修正，不改 GPIO。
- 速度控制 0–100% 已按实机源码确认（`ChangeDutyCycle`），连接后才可点击“应用速度”。

## 协议摘要（实机源码确认，详见 `diagnostics.md`）

| 功能 | 帧（hex） |
|---|---|
| STOP | `FF 00 00 00 FF` |
| 前进 / 后退 / 左转 / 右转 | `FF 00 01 00 FF` / `FF 00 02 00 FF` / `FF 00 03 00 FF` / `FF 00 04 00 FF` |
| 左/右侧速度 0–100 | `FF 02 01 XX FF` / `FF 02 02 XX FF` |
| 舵机 1–8（角度 15–160） | `FF 01 NN AA FF` |
| 云台两轴 | 同上帧格式，通道 7=水平(0–185)、8=垂直(78–170)，实车行程宽于通用 15–160 |
| 心跳 | `FF EF EF EE FF` |

## 配置

界面上的 IP/端口/轮速/互换/视频端口与路径存在浏览器 localStorage（键前缀 `xr.`），
换浏览器或清缓存后回到默认值。`~/.xiaor_remote_config.json` 现在只作为命令行
`--connect` 的默认地址来源，界面不再写它。

## 实车启用前检查清单

0. **供电**：电池组充满并接到扩展板电池接口、打开电源开关。USB 口 5V 只能带树莓派/摄像头，带不动电机（表现为电机只抖不走）。
1. 车轮架空，确认树莓派与 PC 在同一网络（`Test-NetConnection 192.168.88.100 -Port 2001`）。
2. 关闭其它控制端（含旧手机页面），确认 2001 无占用（树上 `ss -lntp | grep :2001`）。
3. 先用 `tools/raw_command_test.py --command all --duration 0.2` 验证四个方向；树上 `tail -f wifirobots.log` 应能看到 `motor_forward/stop` 等逐帧日志。
4. 如左右相反：界面勾选“左右互换校准”（不改 GPIO）。
5. **云台检查**（电池打开后，车上执行）：`python2 /home/liuviking/work/wifirobots/xr_servo_probe.py`——先看电压寄存器是否非零，再观察 1/2 号（左右/上下）扫描时云台是否摆动；纹丝不动则检查舵机三线插头（S1/S2 针座、GVS 朝向）。判定表见脚本头注释。
6. 再落地低速测试，人员远离车轮。
