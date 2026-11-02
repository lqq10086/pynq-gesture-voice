---
name: fpga-project-skeleton
description: 要开一个新的 FPGA / PYNQ 工程、或要把手上乱成一团的工程整理成能交付的样子时使用。提供可直接复制的目录骨架、一键生成脚本、Word 版需求/接口/决策与报告模板、数据目录的管理规则，以及构建、仿真、上板的脚本模板。目的是让第二个人接手时知道每个文件该放哪、每份报告该写什么。不提供具体设计内容。
---

适用场景：项目刚起步要给工程定骨架、交付前要按模板补报告、数据与黄金参考要归档、要统一团队的目录与命名时。


# 工程骨架与文档模板

## 怎么开始

在技能包根目录执行（`--dest` 是目标工程目录，可以指向任何位置）：

```powershell
cd <本技能包目录>
python tools/bootstrap_project.py --dest D:\work\my_project --name my_project --part xc7z020clg400-2
```

它做四件事：复制 `work_sample` 的骨架、建好 `build/ board/ data/ docs/ report/ sim/ src/ skill/ tests/` 等目录、
调同级技能包 eda-script-runner 里的 tools/eda_env.py 把本机工具与版本写进 `<工程>/docs/env.md`、打印下一步命令。
目标目录非空时返回 4（不加 `--force` 不会覆盖）。使用 `--force` 会覆盖目标目录中与 `work_sample/` 同名的文件，包括 `src/` 下的模板文件；`docs/env.md` 也会重新生成。请在空目录中使用，或先备份已有工程。
环境探测依赖同级 `eda-script-runner/tools/eda_env.py`；缺少该文件或 EDA 工具时返回 3，骨架仍会创建，具体缺项见输出与 `docs/env.md`。

生成后先看两份文件：`<工程>/README.md`（骨架说明与首次构建步骤）与 `<工程>/docs/env.md`（这台机器上工具在哪、什么版本）。

## 骨架长什么样

```
my_project/
├── README.md              # 这是什么、怎么构建、怎么跑到板子上
├── docs/                  # requirements / interface / decisions（Word）
├── src/rtl/  src/hls/     # 源码
├── src/constraints/       # XDC：时钟与引脚分开
├── sim/                   # testbench 与运行脚本
├── build/                 # 构建脚本与产物（不进版本库）
├── board/                 # 上板运行与冒烟测试
├── data/input/ data/golden/  # 输入样本与黄金参考，各带 README
├── report/                # 六份报告（Word），一轮一份
├── tests/                 # 可自动化的检查（bootstrap 会建这个目录）
└── skill/                 # 可复用的经验，随项目走
```

命名约定：版本目录 `V<主>.<次>_<主题>_<年月日>`；证据文件 `<范围>_GATE.json`；
报告目录 `reports/<主题>_<时间戳>`，里面固定放 `timing_summary.rpt`、`utilization.rpt`、`drc.rpt`、`cdc.rpt`。
这些名字是给人和脚本共用的，改动前先想清楚谁会读它。

## Word 模板而不是 Markdown

`work_sample/docs/` 与 `work_sample/report/` 里的模板都是 `.docx`：评审、交接、盖章要的是能直接打开的文件。
八个工程内模板：`work_sample/docs/requirements.docx`（需求与验收）、`work_sample/docs/interface.docx`（接口契约）、`work_sample/docs/decisions.docx`（判断与取舍）、
`work_sample/report/design-report.docx`、`work_sample/report/optimization-log.docx`、`work_sample/report/verification-log.docx`、
`work_sample/report/hardware-gate.docx`、`work_sample/report/handoff.docx`。

另有一套更细的空白报告模板：`reports/design-report-template.docx`、`reports/optimization-log-template.docx`、
`reports/verification-log-template.docx`、`reports/hardware-gate-template.docx`、`reports/handoff-template.docx`（design / optimization /

verification / hardware-gate / handoff 五份），它们不是把工程内那五份转格式得到的，
段落与表格更多，用途是"另起一份报告"。`python tools/make_templates.py --out <空目录>` 只会生成工程内那 8 份，
不会覆盖 `reports/*-template.docx`，也不会往已填写的报告里写东西。

交付时把 Word 另存一份 PDF：Word 里"文件 → 导出 → 创建 PDF"，或脚本
`python tools/make_templates.py --pdf <docx>` 之外的入口由你自己接；提交物、评审、交接用 PDF，
需要改内容时回到 docx。两份一起给，不要只给 PDF。

排版统一：A4、页边距左右 3.2 cm 上下 2.5 cm、标题黑体、正文宋体 10.5 pt、拉丁字符 Times New Roman、
表格用 `Table Grid` 样式（有框线）。打印与屏幕阅读都不别扭，AI 读的时候也不会把表格当散文。

## 数据目录的规矩

`work_sample/data/input/README.md` 与 `work_sample/data/golden/README.md` 各写清四件事：放什么、不放什么、
怎么命名、谁在引用它。两条硬规则：换输入或换黄金参考后，之前基于它得出的结论全部作废；
单个文件不超过 50 MB，超了就外链并另写一份来源说明。

登记方式（两份 README 里都写了）：

- data/input/source.md：人读的来源说明——从哪来、什么时候、什么条件、谁授权。
- data/golden/manifest.csv：机器读的清单——`path,sha256_12,source,date,used_by`，换文件必须同时更新。

骨架不预生成空的 CSV（空清单比没有清单更容易被当成"已经登记过"）；第一次放数据时按 README 里的表头新建。

## 脚本模板

| 文件 | 作用 |
| --- | --- |
| `work_sample/build/b01_build_bitstream.tcl` | 综合到比特流，报告落 `build/out/<工程>/<run>/` |
| `work_sample/build/b02_export_hardware.tcl` | 导出 xsa，供软件侧使用 |
| `work_sample/build/b03_collect_reports.tcl` | 从报告抓 WNS/WHS/TNS 与资源占用，汇总成 gate JSON |
| `work_sample/sim/run_sim.tcl` | 跑 testbench，记录波形与日志 |
| `work_sample/board/board_smoke.py` | 上板冒烟：加载 overlay，列出 IP、地址及寄存器名称 |
| `work_sample/board/board_run.py` | 跑一次实际任务，留存耗时与边界标签 |

四个 Tcl 都支持 `-tclargs` 覆盖路径与工程名，缺参时打印 `MISSING_INPUT` 并以 2 退出；
`b03` 解析不到 WNS 返回 2 而不是写空 JSON；`work_sample/board/board_smoke.py` 在找不到位流时也判 3 并说明缺什么。

`board_smoke.py` 会加载位流，改变板上的 FPGA 配置。运行前先确认当前使用摄像头、DMA 等硬件的程序已正常停止、硬件空闲，且目标 bit/HWH 与板卡匹配；无法确认时先不要运行。脚本列出 HWH 中的寄存器名称，不读取寄存器值。
运行前按实际 HWH 填写 `EXPECTED_IP` 中要检查的名称与地址；名称缺失或地址不一致时返回 2。该表留空时脚本会提示补充并返回 3，不加载位流。

Tcl 里的打印一律 ASCII 英文：Vivado 在中文 Windows 上按 GBK 读脚本，UTF-8 写的中文消息会变成乱码。
中文只写在注释里。

## 边界

- 骨架是"能长成完整工程的最小结构"，不是设计本身：RTL、HLS 与约束都要自己写。
- Word 模板只约束结构与必填项，不评判内容好坏。
- 报告里的数字必须来自你自己那次的构建与测量；模板里的数值只是占位示例。
- 需要 Quartus、Gowin 等其他厂商流程时，骨架可用，脚本模板要重写。
