# 基于 PYNQ 的手势与语音交互系统

本工程基于 AX7020、OV5640 与 PYNQ，使用摄像头采集画面，通过手势和语音控制交互界面。PL 负责视频通路和 YOLO-FastestV2 手势网络计算，PS 负责预处理、结果解码、语音识别和应用控制。主界面包含展示 1～4：前三项为空间构建，第四项为绘画小镇。

## 目录

| 路径 | 内容 |
| --- | --- |
| `V5.3-flash_PL01/` | 应用、模型、PL 源码、位流及硬件资料 |
| `V5.3-flash_PL01/reports/` | 位流、硬件描述、执行计划和构建报告 |
| `offline_startup/` | 可配置路径的离线启动桥及测试 |
| `skill/` | PYNQ 工作流程、工程模板和问题排查；贪吃蛇为跨项目复用案例 |
| `tools/restore_q1_dcp.py` | 从合法持有的原工程恢复匹配的检查点 |

核心目录名是工程组成的一部分；原有相对路径和身份校验需保持匹配。`reports/` 保存硬件资料，并非比赛设计报告。

## 环境与设备

原工程使用 AX7020 / Zynq-7020、OV5640、HDMI 显示、PYNQ 3.1.2 和 Python 3.10.4。板端还需要匹配内核的摄像头、显示和 USB 音频驱动。`V5.3-flash_PL01/requirements.txt` 列出部分 Python 依赖；PYNQ 和硬件驱动需由目标系统提供。

语音使用系统的 ALSA 与 `arecord`；当前默认声卡名为 `WirelessMic`。可用 `arecord -l` 和 `cat /proc/asound/cards` 查看设备。其它声卡需按[系统音频说明](V5.3-flash_PL01/docs/system_audio.md)适配并在板端检查。

## 文件校验与 DCP

公开包不含布线检查点 `final_routed.dcp`。静态预检仍核对固定的 bit/HWH、模型、执行计划、加速器接口、HLS 和布线报告；没有 DCP 时，结果会标记 `dcp_validation=NOT_INCLUDED` 和 `route_validation=REPORTS_CHECKED_DCP_NOT_CHECKED`。提供 DCP 时，必须与原构建记录的 SHA-256 一致。

在解压后的核心目录执行以下只读检查（不加载位流、不访问硬件）：

```sh
cd /path/to/gesture_voice_public/V5.3-flash_PL01
python3 -B -c 'import json; from public_accel_preflight import verify_static; print(json.dumps(verify_static("."), indent=2))'
```

静态预检不等于板端验证。持有匹配的原工程或 DCP 时，可在公开包根目录恢复并校验：

```sh
python3 -B tools/restore_q1_dcp.py /path/to/original_project_or_dcp
```

恢复工具要求与原工程记录完全一致的 SHA-256；不接受空文件或其他构建生成的 DCP。

## 板端使用

1. 将公开包放在板端的独立目录，保持上面的目录结构，准备与工程相匹配的 PYNQ 环境和外设。
2. 先完成上述静态预检。切换工程前，按当前设备的交接流程正常停止已有程序并确认硬件已释放；不得删除锁或伪造空闲状态。
3. 在核心目录使用板端 PYNQ 环境的 Python 启动：

   ```sh
   cd /path/to/gesture_voice_public/V5.3-flash_PL01
   sudo /usr/local/share/pynq-venv/bin/python3 -B start_board.py
   ```

   如果目标系统的 PYNQ Python 路径不同，替换为实际解释器。正常停止本工程时，在同一目录运行：

   ```sh
   sudo /usr/local/share/pynq-venv/bin/python3 -B stop_board.py
   ```

   启动和停止程序会分别核验运行状态及硬件交接。
4. 检查显示、手势和语音，再按实际设备调整音频。独立自动启动配置见[离线启动与路径配置](offline_startup/README.md)，该桥需要目标系统具备说明中约定的初始化服务与交接协议。

原工程曾在现用开发板上完成画面、手势、语音及断网启动检查。

## 本地测试

在公开包根目录运行以下测试，检查 DCP 校验、文件身份与恢复工具：

```sh
python3 -B -m unittest discover -s tests
```

启动桥的路径及交接模拟测试见 [离线启动说明](offline_startup/README.md)。这些测试不访问开发板。
## 许可与来源

团队有权授权的自有代码采用 [MIT 许可证](LICENSE)。模型、字体、板级配置、AMD IP 与生成文件按各自来源处理，详见[第三方材料说明](third_party.md)。模型与硬件生成文件的公开分发条件仍需核实后再发布。
