# eda-script-runner 的版本与移植说明

这一页回答两个问题：这些脚本换机器、换 Vivado 版本还能不能用；如果不能，是哪一步先坏。

## 一、本机实测到的工具

`tools/eda_env.py` 在写这份说明的机器上扫到的结果（只读探测，不启动工具）：

| 工具 | 路径 | 版本 |
| --- | --- | --- |
| Vivado | `E:\vivado\Vivado\2023.2\bin\vivado.bat` | 2023.2（`vivado v2023.2 (64-bit)`，`--probe-version` 实测） |
| Vitis | `E:\vivado\Vitis\2023.2\bin\vitis.bat` | 2023.2 |
| Vitis HLS | `E:\vivado\Vitis_HLS\2023.2\bin\vitis_hls.bat` | 2023.2 |
| XSCT | `E:\vivado\Vitis\2023.2\bin\xsct.bat` | 2023.2 |

装在不同盘、不同版本目录都能被扫到，不必改脚本。找不到时先跑一次：

```powershell
python tools/eda_env.py
```

它输出 JSON：`status`、`tools`、`missing`、`need`、`searched_roots`。缺工具时退出码是 3，`need` 里写明缺什么。

## 二、脚本与版本的对应关系

| 脚本 | 兼容范围 | 换版本先坏在哪 | 处理方式 |
| --- | --- | --- | --- |
| `vivado/create_hardware_project.tcl` | 2020.x 到 2025.x 写法一致 | `-flow_target` 之类新选项；`get_files` 对 header 的匹配 | 缺参数会报 MISSING_INPUT；新版本报 unknown option 时删掉该行 |
| `vivado/build_hardware.tcl` | 2020.x 到 2025.x 报告名一致 | `write_hw_platform -fixed`（2019.x 无 `-fixed`）；`report_cdc` 在老版本是 `report_cdc` 的旧签名 | 只报 WARNING，不中断；老版本删 `-fixed` |
| `vivado/report_timing_paths.tcl` | 2020.x 到 2025.x 选项未变 | 无明显断点；源同步端口名换工程后不同 | 用 `-from_pattern`/`-to_pattern` 覆盖 |
| `vivado/delivery_board_ps7.tcl` | 2019.1 到 2023.2 的 xsct | hw_server 未启动、器件型号不同 | 用 `-pattern`、`-url` 覆盖；连不上会打印 MISSING_INPUT |
| `vivado/build_software_vitis_classic.tcl` | **只到 2023.1** | 2023.2 起 xsct 没有 `platform` 命令 | 脚本自己检测并打印 TOOL_INCOMPATIBLE（退出码 3），改用 `vitis -s` |
| `hls/build_ip.tcl` | Vitis HLS 2020.1+；Vivado HLS 2019.x 需去掉 `-flow_target` | 老版本不认识 `-flow_target` | 传 `-flow_target ""` 或删该参数 |
| `hls/cosim.tcl` | 2020.1 到 2025.2 | 没 csynth 就 cosim 会报找不到 solution | 先跑 build_ip.tcl |
| `pynq/boardctl.py` | 任何 PYNQ 2.7 / 3.x 镜像 | 板卡地址、账号、远端根目录不同 | 全部走参数或环境变量 `PYNQ_BOARD` 等 |
| `windows/delivery.ps1` | PowerShell 5.1+ | 旧版只认 2023.1 的四条路径，换机器就 throw | 已改成调用 `tools/eda_env.py` 探测 |

## 三、缺条件时的行为

所有脚本遵循同一条约定：

1. 缺输入、缺工具、缺工程 → 打印 `MISSING_INPUT: <项> <说明>` 和 `NEED: ...`，以 2 退出。
2. 工具版本不符 → 只打印 `WARNING`，继续执行。
3. 工具存在但流程不适用（例如 2023.2 的 xsct 跑 classic 流程）→ 打印 `TOOL_INCOMPATIBLE`，以 3 退出。
4. 工具跑完但结论不达标（时序为负、DRC 阻塞）→ 以 `GATE_FAIL <类别>` 报错退出，这是脚本的目的，不要改成静默通过。

## 三之二、报告文件与解析的约定

- `build_hardware.tcl` 会写两份资源报告：`utilization.rpt`（默认，含 CLB LUTs / CLB Registers / Block RAM Tile / DSPs
  与 Util% 的汇总表）和 `utilization_hier.rpt`（按层级）。只有前者能被 `b03_collect_reports.tcl` 解析出百分比；
  改成只写层级版，百分比就会全为 null。
- 时序表的表头一行写多个指标（WNS(ns)、TNS(ns)、WHS(ns)…），数值在下一行。解析必须按列号取，
  否则 TNS 会读成 WNS 的数——这是本项目里真实发生过的错。
- `b03_collect_reports.tcl` 的 null 有两种含义：该字段解析不到，或该行在报告里根本不存在（例如没有用 BRAM 时
  资源表里没有 Block RAM Tile 行）。要区分就打开 `utilization.rpt` 看一眼，不要假设 0。

## 四、什么时候这套脚本不适用

- 用 Vitis unified（2023.2 之后）做软件构建：classic 脚本整段不适用，需要另写 `vitis -s` 流程。
- 用非 Zynq 的纯 FPGA 器件：`delivery_board_ps7.tcl` 的 Cortex-A9 目标与 `ps7_init` 都不存在。
- 工程用了自定义 IP 且没有对应 XSA/HWH：`build_hardware.tcl` 的 IP 状态检查会失败，属于缺资源不是脚本坏了。
