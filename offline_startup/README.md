# 离线启动与路径配置

本目录提供面向已建立安全交接流程的 PYNQ 系统的启动桥。桥接程序先核对前序服务的当前启动周期、进程和设备所有权，请求正常停止；确认服务退出、硬件空闲及交接状态后，再启动本工程。原有 boot ID、bit/HWH 身份、DMA 和设备占用检查保持启用。

此桥依赖目标系统的 `clear_pl_statefile.service`，以及前序服务按约定写入 `reports/service/status.json` 和 `/run/ax7020-v300-handoff.json`。它适用于实现相同状态和交接协议的系统；新的系统或不同服务需要先完成对应适配和验证。

## 配置工程路径

在板端创建 `/etc/default/gesture-voice`，按实际位置填写：

```ini
GESTURE_VOICE_CORE_ROOT="/home/xilinx/jupyter_notebooks/gesture_voice_public/V5.3-flash_PL01"
GESTURE_VOICE_PREVIOUS_ROOT="/home/xilinx/jupyter_notebooks/V4.5_REDESIGN01"
GESTURE_VOICE_PREVIOUS_UNIT="v45-redesign01.service"
```

- `GESTURE_VOICE_CORE_ROOT` 是本工程核心目录，目录内应有 `board_entry.py` 和 `backend_v51/runtime/owner_guard.py`。
- `GESTURE_VOICE_PREVIOUS_ROOT` 是具备上述交接协议的前序工程目录，服务状态读取自该目录下的 `reports/service/status.json`。
- `GESTURE_VOICE_PREVIOUS_UNIT` 是实际前序 systemd 服务名，必须与该工程和正在运行的进程对应。

如果没有提供配置文件，三个值分别沿用原系统路径和 `v45-redesign01.service`；路径必须是绝对 Linux 路径。程序将路径作为参数直接传递，不通过 shell 执行。目标入口或所有权检查模块不存在时，桥在停止前序服务之前退出。

## 安装服务文件

将本目录的 `bridge.py` 复制到 `/opt/gesture-voice/bridge.py`，将 `v53-nobox.service` 安装为 `/etc/systemd/system/gesture-voice.service`。在公开包根目录操作的示例如下：

```sh
sudo install -d /opt/gesture-voice
sudo install -m 0644 offline_startup/bridge.py /opt/gesture-voice/bridge.py
sudo install -m 0644 offline_startup/v53-nobox.service /etc/systemd/system/gesture-voice.service
sudo systemctl daemon-reload
```

服务默认使用 `/usr/local/share/pynq-venv/bin/python3`。若目标 PYNQ 系统的解释器不同，需要在服务文件中改成实际路径。服务的静态挂载依赖覆盖 `/home` 和 `/opt/gesture-voice`；核心或前序工程位于其他独立挂载点时，应在 `gesture-voice.service` 的 systemd override 中为该挂载点增加 `RequiresMountsFor=`。

确认配置文件、初始化服务及前序服务均符合上述条件，并按工程根目录说明完成模型、硬件文件和语音依赖配置后，再按目标系统的部署流程启用该服务。桥接失败时查看 `journalctl -u gesture-voice.service` 和前序服务状态，按记录排查；不要删除锁、伪造交接状态或强制覆盖仍在运行的硬件所有者。

包内 `V5.3-flash_PL01/deployment/bridge.py` 是核心工程内原有的身份绑定文件；本目录的 `bridge.py` 是此公开版路径配置入口。

## 本地检查

在具有 Python 3 的电脑上，从本目录运行：

```sh
python -B -m unittest test_bridge test_accel_target
```

这些测试模拟配置、进程交接和命令参数，不访问开发板，也不替代目标系统上的启动验证。
