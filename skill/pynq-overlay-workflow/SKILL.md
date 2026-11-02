---
name: pynq-overlay-workflow
description: 为使用 PYNQ 的 PS/PL 工程建立或迁移软硬件分工、Overlay 加载校验、寄存器契约及板端验证流程。用于新应用接入、换板适配或排查 Python 到 PL 的接口问题，不绑定模型、摄像头、游戏或显示方式。
---

# PYNQ 架构与 Overlay 集成

交付能运行、接口明确、可复现的新应用。复用已有系统环境与接口方法，按目标板重建应用硬件；不要把某次工程的 bit、地址、时钟或接线当成通用配置。

## 按任务读取

- 建立或迁移应用：读 [架构与迁移](references/architecture.md)。
- 接入或诊断 Overlay：读 [接口契约与访问](references/contract.md)。
- 板端异常：按 [分层排错](references/troubleshooting.md) 做最小区分实验。
- 整理复用报告：读 [来源与验证边界](references/evidence.md)，区分历史工程证据与新脚本测试。

## 工作顺序

1. **识别环境**。记录板型/SoC、PYNQ/Python、工具版本、实际解释器、启动介质、连接方式、当前 Overlay 与入口。先读已有工程。Notebook、摄像头、网线和浏览器均不是 PYNQ 应用的统一前提。
2. **划分任务**。稳定并行时序、采集和硬件握手放 PL；复杂状态与调度放 PS；上位机按需求可选。少量控制/状态优先寄存器，大块数据才评估 DMA。先算吞吐、缓冲与延迟，不照搬原题链路。
3. **先写契约**。从当前 HWH、RTL、BD、XDC 和手册提取 IP 全名、VLNV、地址范围、偏移/位域/副作用、时钟复位、握手和超时，每个数字注明出处。完整语义可用 [接口附录模板](assets/interface-template.md)；探针 JSON 只表达 IP 与安全读。应用参数、板级校准、接口契约分开保存。
4. **生成匹配产物**。重新核对目标板 PS 配置、器件与约束；同次构建导出 bit/hwh，记录源版本、工具版本、构建标识与哈希。用独立目录和入口保留现有应用。哈希证明文件未变，不单独证明两个文件同次构建。
5. **分阶段接入**。先用 `scripts/check_overlay.py` 离线检查，再在目标 PYNQ 解释器中运行 `scripts/probe_overlay.py`。默认只解析元数据；`--load` 才下载，`--read` 才读取契约声明无副作用的 32 位寄存器。完整 Overlay 会替换当前 PL，按用户任务授权安排，已有授权不重复询问。
6. **隔离驱动与应用**。驱动封装当前硬件语义，业务调用高层方法。事件保序；可合并的显示/遥测只保留最新待发项。toggle/ACK 必须按 RTL 等待受理，不能连续翻转抵消未完成提交。超时、溢出和失联采用应用规定的处理方式。
7. **分层验证和交接**。分别记录离线、软件/mock、RTL、综合实现、实际加载、真实外设和完整链路。接口/环境变化才扩大回归。源码、产物、配置来源、启动停止、依赖和未测项随工程交付，使用 [验证记录模板](assets/validation-template.md)。

## 非显然的操作边界

- `Overlay(..., download=False)` 不下载；`is_loaded()` 是 PYNQ 状态，HWH 是设计元数据，不能替代独立 PL 身份读回。
- 寄存器读取可能清状态或弹出 FIFO。不要遍历地址、批量打印整个 `register_map` 或猜地址探测；先确认副作用。
- Linux 正在运行时，不用裸机 `ps7_init`、硬编码 SLCR 写入或全局复位作常规修复。按当前板型核查时钟复位。
- SSH/UART 可替换；解释器、账号、目录、串口与波特率来自当前环境，不内置凭据，不反复重刷系统代替定位。
- 无实板时继续实现与隔离验证，板测写 `NOT_RUN`。换板需适配和重验，不宣称零修改兼容所有设备。

## 工具入口

从 [契约模板](assets/contract.template.json) 建立项目 JSON，填写所有 null 和示例名称；`safe_reads` 可为空。

```text
python scripts/check_overlay.py PROJECT/overlay-contract.json
python scripts/probe_overlay.py PROJECT/overlay-contract.json
python scripts/probe_overlay.py PROJECT/overlay-contract.json --load
python scripts/probe_overlay.py PROJECT/overlay-contract.json --read
python scripts/test_helpers.py
```

命令相对于本 Skill，JSON 内路径相对于 JSON。两支检查工具输出 JSON，正常执行的结果码为 0=本项 PASS、1=FAIL、2=NOT_RUN；命令行用法错误由 argparse 报错。test_helpers.py 输出 unittest 文本。离线 PASS 不表示硬件 PASS；探针不初始化应用寄存器，不验证引脚、真实频率或布线时序。
