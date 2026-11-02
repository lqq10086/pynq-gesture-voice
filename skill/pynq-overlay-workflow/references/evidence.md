# 来源与验证边界

本 Skill 面向手势与语音交互参赛作品，于 2026-11-01 结合项目中的架构和调试经验整理。贪吃蛇是独立的跨项目复用案例，用于展示这些方法在另一类应用中的使用。

| 来源 | 共性 | 留在案例的参数 |
|---|---|---|
| 参赛作品：手势与语音交互系统 | 文本源重建、PS/PL、Overlay、寄存器/缓冲、交接 | 摄像头/模型/视频/三维业务、个人路径和 SSH 配置 |
| 跨项目复用案例：贪吃蛇工程 | 基础环境接入新应用、独立 Overlay、MMIO、事件与显示、UART 部署与分层验证 | AX7020 PS7/SLCR、J11、GPIO 地址、0xA5、六键/数码管校准 |

课程案例使用 AX7020、PYNQ3.1.2、Vivado2023.1，新增 PL 按键/显示和 Python 游戏逻辑，复用原有平台方法。历史记录有实际生成的 bit/hwh、PC158项软件、ARM127项软件、RTL21项检查及真实外设/UART浏览器确认。软件含 mock/虚拟时间，不能相加称实体测试。2026-10-31 重启快照记录真实 UART、waiting、fault=null，仅证明该次状态。

本包脚本对不同布局和错误输入进行了隔离软件测试。项目板端记录保留在对应工程中，新工程按实际硬件契约记录验证结果。

## 从失败提炼

- 实体编号不按 KEY 顺序、显示只亮小数点：资料核对和实物校准分开。
- 连续 toggle 可抵消未受理请求：按具体协议处理 ACK，不普遍套用。
- UART 可见但无法打开/突然消失：连接、Linux、PYNQ、Overlay、外设逐层定位。
- 原搬运脚本有个人路径和辅助模块依赖：新脚本使用显式契约与标准库，解释器由现场选择。
- 内部时序、PC/ARM热点与真实链路指标不同：结果按证据层记录。

## 原始记录

课程目录 Task4_pynq_snake：scripts/create_project.tcl、snakegame/hardware.py、docs/VALIDATION.md、reports/build_result.txt、board_arm_tests.log、rtl_result.log、restart_20261031_result.json。比赛技能材料 skill/3 提供构建与 PYNQ 搬运器的历史来源，不是本包依赖。

verification/source-manifest.json 保存本次所读来源的文件名和 SHA256，便于原工程旁复核；发布包不复制应用、商业工具与镜像。新脚本重新编写，结果见 verification。
