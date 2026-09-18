# xiaor-remote — 小R科技树莓派 WiFi 小车 Windows WASD 遥控客户端

通过 Windows PC 的 WASD 键盘，经持久 TCP socket 遥控小R科技（XiaoR GEEK）树莓派 WiFi 视频小车。
**复用原厂 `wifirobots.py` 的 2001 控制服务，不改 GPIO、不覆盖原厂固件。**

```text
Windows GUI / 键盘
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
| WASD 客户端（Phase 2） | ✅ 完成，`src/` |
| 安全完善 + 单元测试（Phase 3） | ✅ 完成，`tests/`（本地假服务器端到端验证） |
| 速度 / 云台舵机 / 车灯 UI | ✅ 完成（协议已按实机源码实现）；云台 2026-09-12 专项诊断：手机页面→固件→I2C 舵机板逐层实测正常，云台不动为舵机接线/供电问题，见 `pi/xr_servo_probe.py` |
| MJPEG 视频（Phase 4） | ✅ 客户端已实现并隔离线程；**2026-09-12 实机验证出图**（USB 摄像头 + 8080 自动启动）；`tools/fake_robot.py` 可联调 |
| 手机网页遥控（树莓派侧） | ✅ 已开发并部署，`http://192.168.1.1:8082/`，rc.local 开机自启 |
| 本地假机器人 | ✅ `tools/fake_robot.py`（无硬件联调 GUI/工具） |
| Windows exe 打包 | ✅ `build_exe.bat` + `dist/xiaor-remote/`（已冒烟验证） |
| 实车运动验证 | ⚠️ 软件链路已确认（方向指示灯 + 固件 `wifirobots.log` 逐帧）；2026-09-12 实测直连 TCP 前进 3 秒时电机只在 USB 5V 供电下抖动 —— 必须用电池组（约 7.4V）供电后复验 |
| 服务端看门狗 | ⏸ 未做（固件无看门狗，安全全部由客户端保证；改动固件前必须备份） |

## 目录结构

```text
liuviking/
├─ README.md                 本文件
├─ ROBOT_INFO.md             主机连接信息（IP/口令/协议/变更记录）
├─ requirements.txt          GUI 依赖（PySide6）
├─ diagnostics.md            Phase 0 实机勘查报告（系统/进程/协议/视频）
├─ TEST_REPORT.md            测试报告（单元测试 + 实机 + 假机器人联调）
├─ app.py                    顶层入口（python app.py，打包用）
├─ run_gui.bat               双击启动 GUI
├─ build_exe.bat             双击打包 exe（PyInstaller）
├─ dist/xiaor-remote/        已打包的 Windows 程序
├─ pi/
│  ├─ web_controller.py      树莓派手机网页遥控服务（Python2/3 兼容，仅标准库）
│  └─ start_web_controller.sh 树莓派启动脚本（rc.local 调用）
├─ src/
│  ├─ main.py                程序入口（python src/main.py）
│  ├─ protocol.py            5 字节协议帧 + 增量解析（与实机源码逐字节对齐）
│  ├─ robot_client.py        线程安全 TCP 客户端（心跳/断线处理/左右校准）
│  ├─ keyboard_controller.py WASD 多键状态机（最后按下优先）
│  ├─ video_client.py        MJPEG 读取（独立线程，失败不影响遥控）
│  ├─ ui.py                  PySide6 界面与安全逻辑
│  └─ config.py              配置持久化（~/.xiaor_remote_config.json）
├─ tools/
│  ├─ raw_command_test.py    单方向短脉冲协议验证（到时自动 STOP）
│  ├─ probe_robot.py         TCP 2001 / 心跳 / MJPEG 探测
│  └─ fake_robot.py          本地假机器人（2001 + MJPEG，无硬件联调）
└─ tests/
   ├─ test_protocol.py       帧字节、速度/舵机钳制、流/增量解析
   ├─ test_key_state.py      多键优先级、松键回退
   ├─ test_robot_client.py   本机假机器人服务器的端到端测试
   └─ test_video_client.py   MJPEG boundary 提取与分片解析
```

## 快速开始

### 1. 安装依赖（仅 GUI 需要）

```powershell
python -m pip install -r requirements.txt
```

`tools/` 与 `tests/` 只用 Python 标准库，不装 PySide6 也能跑。

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

### 4. 启动 WASD 客户端

```powershell
python src/main.py                 # 或 python app.py / python -m src.main
python src/main.py --host 192.168.88.100 --connect
python src/main.py --host 192.168.88.100 --connect --video-url "http://192.168.88.100:8080/?action=stream"
```

也可以在资源管理器里双击 `run_gui.bat`，或直接双击已打包的
`dist\xiaor-remote\xiaor-remote.exe`（整个文件夹需保持在一起）。

### 5. 手机操控（树莓派内置网页遥控，推荐）

树莓派上已部署 `pi/web_controller.py`（端口 8082，rc.local 开机自启）。

1. 手机 WiFi 连接小车热点 `wifi-robots.com_b827ebf61386`，密码 `12345678`（提示“无互联网”属正常）。
2. 手机浏览器打开 `http://192.168.1.1:8082/`。
3. 按住方向键移动，松开／切后台立即停止；红色 STOP 为急停。桌面浏览器也可用 W/A/S/D + 空格。
4. **转向方式**：页面”转向:弧线/原地”按钮切换（默认弧线）。弧线= A/D 边走边转（前进命令 + 内侧轮 30%、外侧轮 80% 差速）；原地=原来的单轮反转原地转。W/S 始终直行；每次运动前自动恢复对称速度，异常停车后服务端也会把速度复位 100/100。
5. 摄像头已可用（2026-09-12 实测）：点”视频”看 MJPEG（默认 `:8080/?action=stream`）。Android 浏览器正常；iOS Safari 不支持 MJPEG `<img>`，需用其它端。

安全与并发：

- 看门狗：3 秒无页面活动自动 STOP 并断开 2001，把控制权让给官方 APP；
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

# 终端 2：GUI 连假机器人并开视频
python src/main.py --host 127.0.0.1 --connect --video-url "http://127.0.0.1:8080/?action=stream"
```

假机器人会把收到的每一帧解码打印（`运动 STOP`、`速度 左侧 80%`、`舵机 1 -> 90°` 等），
可据此核对 GUI/工具发出的内容。

### 7. 界面功能说明

- **速度**：左右轮 0–100%，连接后点击“应用速度”。（协议已按实机源码确认）
- **云台**：选择舵机号 1–8，拖动角度 15–160°，点“应用”或“复位 90°”。
- **车灯**：开灯 / 关灯（LED0）。
- **视频**：填视频端口（默认 8080）与路径（默认 `/?action=stream`），勾选“启用视频”。
  视频在独立线程读取，**视频不可用完全不影响遥控与急停**；连接断开自动停止视频。

### 8. 运行单元测试

```powershell
python -m unittest discover -s tests -v
```

### 9. 打包 exe（可选，已提供成品）

```powershell
build_exe.bat      # PyInstaller：产物在 dist\xiaor-remote\
```

## 操作与安全设计

| 操作 | 行为 |
|---|---|
| W / S / A / D | 前进 / 后退 / 左转 / 右转（按住持续，松开即停） |
| 多键同时按 | 最后按下的方向优先；松开后回退到仍按住的方向，全部松开才 STOP |
| 按住期间的持续刷新 | 手机页面每 0.4s 重复发送当前方向；服务端 1.2s 收不到刷新自动 STOP（页面被杀/断网/丢事件时电机最多再跑约 1.2s） |
| GUI 运动刷新看门狗 | Windows 客户端按住方向键期间靠按键自动重复喂狗，1.2s 无刷新自动急停（防 keyup 被拖动窗口/系统弹窗吞掉后跑飞） |
| 急停必达 | 手机页面 STOP 即使在桥与固件连接已断时也会先重连再送 STOP |
| Space | 紧急停车（任何聚焦状态下都生效） |
| Esc | 停止 + 断开连接 |
| 窗口失焦（Alt+Tab） | 自动 STOP（可在界面关闭） |
| 关闭窗口 / 程序退出 | best-effort STOP 后断开 |
| 网络异常 | 状态变红“连接错误”，清空按键，拒绝继续发送运动指令 |
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
| 心跳 | `FF EF EF EE FF` |

## 配置

界面上的 IP/端口/速度/校准项自动保存到 `~/.xiaor_remote_config.json`，下次启动自动载入。

## 实车启用前检查清单

0. **供电**：电池组充满并接到扩展板电池接口、打开电源开关。USB 口 5V 只能带树莓派/摄像头，带不动电机（表现为电机只抖不走）。
1. 车轮架空，确认树莓派与 PC 在同一网络（`Test-NetConnection 192.168.88.100 -Port 2001`）。
2. 关闭其它控制端（含旧手机页面），确认 2001 无占用（树上 `ss -lntp | grep :2001`）。
3. 先用 `tools/raw_command_test.py --command all --duration 0.2` 验证四个方向；树上 `tail -f wifirobots.log` 应能看到 `motor_forward/stop` 等逐帧日志。
4. 如左右相反：界面勾选“左右互换校准”（不改 GPIO）。
5. **云台检查**（电池打开后，车上执行）：`python2 /home/liuviking/work/wifirobots/xr_servo_probe.py`——先看电压寄存器是否非零，再观察 1/2 号（左右/上下）扫描时云台是否摆动；纹丝不动则检查舵机三线插头（S1/S2 针座、GVS 朝向）。判定表见脚本头注释。
6. 再落地低速测试，人员远离车轮。
