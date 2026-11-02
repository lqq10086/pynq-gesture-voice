# 通用 PYNQ Skill

本目录是手势与语音交互参赛作品的通用 PYNQ 技能入口，具体流程见 `SKILL.md`。内容围绕项目中的 Overlay、寄存器和软硬件接入经验整理；贪吃蛇是独立的跨项目复用案例，用来展示该方法在另一类应用中的使用。

## 适用范围与使用方法

适用于已有受支持 PYNQ/Linux 环境、通过 Overlay 和寄存器访问 PL 的单板工程。复制到支持 Agent Skills 的工具目录，或让 agent 读取 `SKILL.md`；Codex 用 `$pynq-overlay-workflow` 调用。

示例请求：

> 使用 $pynq-overlay-workflow，在现有 PYNQ 系统上接入一个新的传感器采集与控制应用。先读取工程与目标板资料，建立契约，再完成独立 Overlay、Python 驱动、启动入口和验证记录，保留现有应用。

也可人工执行流程。离线脚本仅需 Python 3.9+ 标准库；板端探针另需当前环境的 PYNQ。模板不是可直接上板的硬件配置，地址、类型和哈希需填入。

## 内容与赛道对应

| 文件 | 可复用内容 | 边界 |
|---|---|---|
| `SKILL.md` | 分工、接入、部署与验证工作流 | 用户选择与现有工程优先 |
| `assets/contract.template.json` | Overlay/IP/安全读契约 | 参数来自当前硬件 |
| `assets/interface-template.md` | 时钟复位、读写副作用、握手和缓冲契约 | 未确定值标 UNKNOWN，不由探针猜测 |
| `scripts/check_overlay.py` | 离线文件与契约检查 | 不证明硬件正确 |
| `scripts/probe_overlay.py` | 参数化加载/IP/安全寄存器校验 | 不自动初始化，不扫描地址 |
| `references/contract.md` | MMIO 与 register_map 范式 | 访问前确认副作用 |
| `references/troubleshooting.md` | 分层定位与停止条件 | 专属动作回到板卡手册 |
| `references/evidence.md` | 历史案例、失败观察与验证范围 | 新旧证据分开 |
| `assets/validation-template.md` | 验证与交接记录 | 未做写 NOT_RUN |

对应“通用 PYNQ Skill 单独加分”：主入口与脚本不要求游戏、模型、摄像头、拓展口或固定地址；换题目沿用流程，换板卡填写新契约并适配硬件。DMA 是条件分支，本包没有交付经过实板验证的通用 DMA 封装。

## 已验证效果

手势与语音交互作品保留了构建、Overlay 接入及应用验证记录。作为跨项目复用案例，独立贪吃蛇工程记录了 bit/hwh、PYNQ 加载、实体外设与 UART 浏览器链路。新脚本以不同布局和故障注入进行隔离软件测试，见 `verification/helper-tests.txt`；独立使用评估见 `verification/forward-test.md`。

本 Skill 于 2026-11-01 根据两个项目的接入和调试经验整理，案例记录和脚本测试分别保存，便于对照使用。

## 失效条件

PYNQ 后端不支持所用 bit/HWH、`ip_dict` API 变化、非 32 位访问、动态局部重配置或多板共享资源时，需要适配。跨板、跨镜像、跨工具版本均需重验。脚本不修复缺时钟、复位、设备树、供电或接线，也不重建操作系统。

## 来源

见 `references/evidence.md` 与 `verification/source-manifest.json`。英文文件名、中文说明，便于提交与阅读；不携带凭据、商业工具、系统镜像和项目专用 bitstream。
