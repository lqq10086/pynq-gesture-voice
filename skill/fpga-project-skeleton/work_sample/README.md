# work_sample 工程骨架

## 这是什么

一个可以直接复制改名的工作目录骨架，用于 AMD FPGA 与 PYNQ 类作品。它规定"东西放哪里、叫什么名字、
每一步产出什么文件"，不规定具体设计。

复制方法：把整个 `work_sample` 目录复制到你的工程位置，改名为你的项目名（小写英文加连字符），
然后按下面第五节把占位文件替换成真实内容。

## 目录结构

```
work_sample/
  README.md              本文件，项目说明与复现入口
  build/
    b01_build_bitstream.tcl   综合、实现、导出 bit 与 HWH
    b02_export_hardware.tcl   导出 XSA，供 Vitis 或 PYNQ 使用
    b03_collect_reports.tcl   把一次构建的报告汇总成 gate_summary.json
  src/
    rtl/                 手写 RTL
    hls/                 HLS 源码
    constraints/         XDC 约束
  sim/
    tb/                  测试平台
    run_sim.tcl          跑仿真并保存日志
  board/
    board_smoke.py       加载 overlay 并检查 IP 与基地址
    board_run.py         板上正式运行入口
  data/
    input/               固定输入样本，必须可复现
    golden/              黄金参考输出
  report/
    README.md              本目录说明与模板指针
    design-report.docx     设计报告实例
    optimization-log.docx  优化记录实例
    verification-log.docx  验证日志实例，按证据层填写
    hardware-gate.docx     硬件门禁结论实例
    handoff.docx           交接说明实例
  skill/                 本项目的技能包（与本次交付的技能包同构）
  docs/
    requirements.docx      需求，逐条编号
    interface.docx         寄存器与接口契约
    decisions.docx         关键决策与理由
```

## 三条命名与放置规则

1. 目录名与文件名一律小写英文、数字、连字符或下划线。中文只出现在文件正文。
2. 生成物不进源码目录。bit、HWH、报告、日志分别落在各自目录，构建脚本负责落位。
3. 每个目录都有 README 或占位说明，说明这一层放什么、谁生成、能否删除。

## 文件头的统一格式

所有脚本与 RTL 文件开头写同一段注释，缺任何一行都不算完成：

```text
# 用途：
# 输入：
# 输出：
# 退出码：0 成功，2 判定不通过，3 证据不足
# 工具版本：
# 作者与日期：
# 未验证项：
```

`未验证项` 这一行最容易被省略，也最重要。它告诉接手的人哪些结论只是推断。

## 从骨架到真实工程

1. 先填 `docs/requirements.docx`，逐条编号。没有需求编号的任务不要开工。
2. 再填 `docs/interface.docx`，寄存器表带出处行号。硬件侧与主机侧都引用这一份。
3. 用 `build/b01_build_bitstream.tcl` 出一个能跑通的最小工程，先不接算法。
4. 加算法时，每加一块就更新 `report/verification-log.docx`，按证据层记录。
5. 每个版本的 bit、HWH、模型、decoder 四个哈希记进 `report/hardware-gate.docx`。
6. 交付前按 `report/verification-log.docx` 的最后一张表逐项打勾。

## 占位文件说明

骨架里的 `.template` 文件是格式示例，不能直接运行。替换后把后缀去掉。

- `src/rtl/top_template.v`：模块头、端口分组、时序逻辑的写法示例。
- `src/hls/kernel_template.cpp`：HLS 顶层函数的接口与 pragma 放置位置示例。
- `sim/tb/tb_template.v`：测试平台的时钟、复位、超时、断言四段结构。
- `report/*.docx`：每份报告的分节与表格格式，正文留空。

## 不包含什么

- 具体器件型号、板卡引脚、时钟频率。这些随板卡变化，必须在 `src/constraints/` 与
  `build/b01_build_bitstream.tcl` 的参数区自己填。
- 任何模型文件与数据集。放在 `data/`，并登记哈希。
- 技能包正文。`skill/` 目录结构与本次交付的技能包一致，可直接复制。
