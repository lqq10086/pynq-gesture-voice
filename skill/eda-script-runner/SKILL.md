---
name: eda-script-runner
description: 在 Windows 上跑 Vivado / Vitis / Vitis HLS / XSCT 的批处理脚本时使用：不知道工具装在哪、脚本报 invalid command、缺工程或缺参数、要在别的机器上复现构建、想知道某个脚本换了 Vivado 版本还能不能用。提供工具探测、统一入口、参数化脚本与版本矩阵，缺条件时明确说缺什么而不是直接报错。不用于设计本身，也不替代厂商文档。
---

适用场景：第一次在某台机器上跑构建、脚本在别人机器上能跑在这台不能、换 Vivado 版本后报错、需要在 CI 或无记忆环境里跑一次构建时。


# EDA 脚本运行

## 先做这一步

```powershell
python tools/eda_env.py
```

它扫盘、读环境变量、查 PATH，输出 `status`、`tools`、`missing`、`need`、`searched_roots`。
工具齐全时退出码 0；缺工具时退出码 3，`need` 里写明缺什么。不要凭印象判断"装了没有"。

本机实测样例（换机器会不同）：

```
vivado: E:\vivado\Vivado\2023.2\bin\vivado.bat  (版本 2023.2)
vitis:  E:\vivado\Vitis\2023.2\bin\vitis.bat
hls:    E:\vivado\Vitis_HLS\2023.2\bin\vitis_hls.bat
xsct:   E:\vivado\Vitis\2023.2\bin\xsct.bat      (版本探针无效，见下)
```

`xsct --probe-version` 不返回版本（只回 `ECHO is off.`）。要确认 xsct 版本，用
`xsct.bat` 跑一句 `puts [version]`，别信探测结果为空就当成没装。

## 再跑脚本

统一入口，负责挑工具、拼命令行、落日志、区分"缺环境"与"缺输入"：

```powershell
python tools/run_tcl.py vivado/create_hardware_project.tcl --cwd "$env:TEMP\eda-work" `
    --args "-part xc7z020clg400-2 -project_dir D:\proj\build\proj -rtl_dir D:\proj\src\rtl"
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--dry-run` | 只打印将执行的命令，不跑 |
| `--tool vivado/hls/vitis/xsct` | 覆盖自动推断 |
| `--cwd <空目录>` | 工具的当前目录；**不给就用系统临时目录**，避免 `vivado.log`/`.Xil` 落在技能包里 |
| `--log <路径>` | 日志位置；不给就写 `%TEMP%\eda-skill-logs\` |
| `--timeout <秒>` | 超时，默认 7200 |
| `--args "..."` | 原样转发给 `-tclargs` |

已知限制：`--args` 按空格切分，**路径里不要带空格**；带空格时改用短路径或先 `--cwd` 进目录再给相对路径。

## 退出码约定

| 码 | 含义 | 下一步 |
| --- | --- | --- |
| 0 | `OK`（工具退出码 0 且输出无 ERROR / GATE_FAIL）或 `WARN`（退出码 0 且出现 `CRITICAL WARNING`） | `WARN` 时先确认产物是否已写出 |
| 2 | 脚本判失败：`MISSING_INPUT`（缺输入）、`SCRIPT_FAILED`（`ERROR:` / `GATE_FAIL`）、`TIMEOUT` | 补齐 `NEED` 行；`GATE_FAIL` 看类别 |
| 3 | 缺工具，或工具版本不支持该流程（`TOOL_INCOMPATIBLE`） | 把 `need` 转给上级，别反复重试 |
| 4 | 脚本文件本身不存在或参数错 | 检查路径 |

Vivado 的 `.bat` 包装器可能在生成产物后仍以非零状态退出；包装器将其报告为失败，
需要检查日志、工具退出码和预期产物，不能仅凭缺少 `ERROR:` 就判定成功。

## 脚本清单

| 脚本 | 做什么 | 需要什么 |
| --- | --- | --- |
| `vivado/create_hardware_project.tcl` | 从源文件新建工程 | `-part`、`-project_dir`、`-rtl_dir`，可选 `-xdc`、`-bd`、`-top` |
| `vivado/build_hardware.tcl` | 综合、实现、比特流、报告、xsa | 已有 `.xpr`；负裕量或 DRC 阻塞时以 `GATE_FAIL` 失败 |
| `vivado/report_timing_paths.tcl` | 只读导出时序路径与时钟 | 布线后 DCP；端口匹配可用 `-from_pattern` 覆盖 |
| `vivado/delivery_board_ps7.tcl` | JTAG 探测、读状态、下载、复位 | 上电板卡与 hw_server；器件用 `-pattern` 覆盖 |
| `vivado/build_software_vitis_classic.tcl` | platform/BSP/FSBL/应用 | XSA 与源码目录；**只适用 Vitis classic（≤2023.1）**，2023.2 上打印 `TOOL_INCOMPATIBLE` |
| `hls/build_ip.tcl` | C 仿真、综合、导出 IP | HLS 工程、顶层、源码、器件、时钟周期 |
| `hls/cosim.tcl` | C/RTL 协同仿真 | 已 csynth 的 solution |
| `pynq/boardctl.py` | SSH 执行与传输 | 板卡地址（`--board` 或 `PYNQ_BOARD`） |
| `windows/delivery.ps1` | 交付检查与烧写 | 工具由 `tools/eda_env.py` 探测后调用 |

公共文件 `tools/tcl_preamble.tcl`：所有 Tcl 脚本先 source 它，由它解析 `-tclargs`、
校验必填输入、识别版本；缺参数时打印 `MISSING_INPUT` 而不是让工具报 invalid command。

## 实测到什么程度

本机（Vivado 2023.2，`E:\vivado`）真跑过的是两条链路：

1. `vivado/create_hardware_project.tcl` 建一个只含 24 位计数器与 XDC 的工程 → 8 秒，退出 0。
2. `vivado/build_hardware.tcl` 综合 + 实现 + 比特流 → 85 秒，产出 `.bit`、`.xsa` 与 10 份报告。

没跑过的是 `vivado/delivery_board_ps7.tcl`（无 JTAG 板卡）、`vivado/build_software_vitis_classic.tcl`
（2023.2 上没有 classic 流程）、`hls/*`（本机无现成 HLS 工程）。这些脚本的参数化是照着
同名命令行改的，第一次用请先用 `--dry-run` 看一眼拼出来的命令。

两个在实测中暴露的工程约束，写进脚本也写在这里：

- 纯 RTL 工程的 `xsa` 里没有 `.hwh`（没有 PS handoff），不能直接给 Vitis/PYNQ 用；
  `vivado/build_hardware.tcl` 会打一行 `NOTE:`，别把这种 xsa 当交付物。
- 工程放在含中文的路径下时，Vivado 的综合子进程可能报
  `TclStackFree: incorrect freePtr. Call out of sequence?`；本轮复现后换纯 ASCII 路径解决。
  `tools/run_tcl.py` 会告警但不阻止，你自己决定。

## 版本与移植

版本矩阵、每个脚本"换版本先坏在哪"、不适用场景写在 `notes/portability.md`。
要点：Vivado 2020.x–2025.x 的命令与报告名基本一致；Vitis classic 在 2023.2 之后不可用；
脚本不写死安装路径与工程名，缺省值只是某次运行的值。

另有一条与语言有关的坑：Tcl 文件里**不要写中文消息**。Vivado 在中文 Windows 上按系统编码
（GBK）读脚本，UTF-8 写的中文在 Tcl 里已经是乱码，`puts` 出来必然乱码。
本包所有 `.tcl` 的打印一律 ASCII 英文，中文只出现在注释里（注释不打印，不参与判定）。

## 三条判断

1. **工具跑完不等于可以部署**：负裕量或阻塞 DRC 时脚本主动失败，这是它的用途，别改成只告警。
2. **能跑通一次不等于可移植**：换机器先 `tools/eda_env.py`，再看 `MISSING_INPUT`。
3. **`WARN` 不是成功也不是失败**：先核产物，再决定要不要重跑。

## 边界

- 脚本是"某次真实工程里跑通过的写法"，不是厂商支持的产品；升级工具后先跑最小用例。
- 不含 IP 设计、时序收敛策略、板级电路设计，这些看 `fpga-project-skeleton` 的骨架与报告模板。
- 板卡相关步骤需要真实硬件；没有硬件时脚本打印 `MISSING_INPUT`，不要伪造结果。
