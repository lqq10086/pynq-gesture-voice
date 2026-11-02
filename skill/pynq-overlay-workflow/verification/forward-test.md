# PYNQ 温度记录器独立前向试用结果

日期：2026-11-01。使用 Skill：`C:/Users/user/.codex/skills/pynq-overlay-workflow/SKILL.md`，读取 architecture、contract、evidence 参考文档和契约/验证模板。本次实际套用 Skill 处理新接口；没有把历史工程测试当作本项目证据。

## 结论

已检查离线样本、建立契约与寄存器语义清单、准备只读身份验证的 Python 适配骨架和后续接入计划。离线文件/契约 PASS；提供的隔离测试 19 项通过；本项目 XML 人工结构核对与 mock 通过。目标板、真实 HWH 解析、实际下载、真实 MMIO、温度采集与新板迁移均为 NOT_RUN。

**这份交付是合成样本，不能上板。** `measure.bit` 为 60 字节文本，明确包含 `SYNTHETIC FOR OFFLINE FILE TEST ONLY, NOT FPGA CONFIGURATION`；116 字节 `measure.hwh` 的根元素是 `SYNTHETIC_BOARD`，不是已验证可供 PYNQ 解析的真实 Vivado HWH。离线 PASS 不意味着可部署。

## 输入与环境边界

- 输入仅为 `requirements.md`、`hashes.json`、`overlay/measure.bit`、`overlay/measure.hwh`。原文件未改动。
- 已知目标 SoC 类别为 Zynq UltraScale+；确切板卡、器件、PS/DDR/MIO、时钟复位、XDC、工具与源版本均未提供。没有套用 PS7 或 SLCR 配置。
- Windows 主机实际解释器：`C:/Users/user/AppData/Local/Programs/Python/Python39/python.exe`，版本 `Python 3.9.12`。这只是本次离线运行解释器，不能推定目标板解释器、PYNQ 版本、账号、路径、SSH/UART 参数。
- 本次没有实板、UI 或外部服务访问，没有重写启动介质，也没有执行 `--load` 或真实 `--read`。
- 所有新增工作文件和测试临时目录位于本 fixture。Python 使用 `-B`，子进程设置 `PYTHONDONTWRITEBYTECODE=1`，`TEMP/TMP` 指向 fixture 的 `test-tmp`，避免写入 Skill 目录或其他临时位置。

## 交付与契约

- `overlay-contract.json`：符合 schema_version=1，路径相对 JSON，build_id=`synthetic-forward-1`。target_label 明确标注精确板卡未确认、SYNTHETIC、DO-NOT-LOAD。
- `interface-registers.json`：保留全部已知寄存器语义及待补项，未知宽度、位域、命令编码等保持 null，没有编造。
- `app/temperature_interface.py`：依赖注入形式的 Python 草稿，无 PYNQ 导入、下载或初始化操作。仅实现已加载 Overlay 的全量 IP 元数据核对和指定 ID 读回；start/capture 明确抛出 NotImplementedError，待补 RTL 协议后实现。不是能运行采集的完整记录器。
- `fixture_mock_check.py`：本项目结构/调用边界验证；`run-forward.py`：命令与日志收集。
- `reports/*.json`：保存每条命令完整 argv、工作目录、退出码、stdout、stderr。`helper-sources.json` 是执行后采集的辅助脚本 SHA256，不能倒推证明执行期间文件未被其他并行任务修改。

| 项目 | 当前资料 | 数值出处/使用范围 |
|---|---|---|
| 顶层 IP / VLNV | samples_0 / lab:ip:samples:1.0 | requirements.md 第 3 行及合成 HWH 第 1 行 |
| 物理基址 / 范围 | 0xA0100000 / 0x1000 | 同上；mock 参数，未实际 MMIO |
| CONTROL | 0x00，启动命令 | requirements.md 第 3 行；编码/宽度/ACK 未知，不写 |
| STATUS | 0x04，读取清事件 | 同上；不列入 safe_reads，不探测 |
| DATA | 0x08，读取弹出 FIFO | 同上；不列入 safe_reads，不探测 |
| ID | 0x14，32 位，无读副作用，固定 0x504E5951 | 同上；唯一 safe_reads 项，掩码 0xFFFFFFFF 为覆盖所声明 32 位值 |

清单中的 SHA256 与文件计算结果一致：

```text
bit  0b4340601d837d945cb1203d9a4e44323be958476a831dc72b502f134863a331
hwh  7daf0f282c6d511113aa396a59a3520356027d02995082355475e35f02e46e12
```

标签和哈希只证明当前离线样本与给定清单一致。没有源提交、真实构建报告和导出记录，不能由哈希或同名配对证明 bit/hwh 来自同次真实构建。

## 实际命令与结果

以下 `PY` 为上述主机 Python 的完整路径，`SKILL` 为上述 Skill 目录；实际完整 argv 见各日志，不使用这些符号作为目标板路径。

| 实际调用 | 退出码与结果 | 日志 | 可证明范围 |
|---|---|---|---|
| `PY -B SKILL/scripts/check_overlay.py overlay-contract.json` | 0，PASS；ip_count=1，safe_read_count=1，board_test=NOT_RUN | reports/offline.json | 文件存在、相对路径配对、给定哈希、XML 语法、契约字段/对齐/范围 |
| `PY -B SKILL/scripts/test_helpers.py` | 0；19 tests，0.673s，OK | reports/helpers.json | 提供的隔离行为回归；包含替代布局、偏移拒绝、元数据拒绝、身份不匹配停止后续读等；不是板测 |
| `PY -B SKILL/scripts/probe_overlay.py overlay-contract.json` | 2，NOT_RUN | reports/probe_metadata.json | 当前主机没有 pynq：`No module named 'pynq'`；未进入真实 Overlay 元数据解析 |
| `PY -B fixture_mock_check.py` | 0，PASS | reports/fixture_mock.json | 本 fixture XML 中 IP 与契约一致；合成标记确认；默认无 MMIO；选定安全读及适配骨架仅访问 ID |

默认 mock 调用为 `Overlay(download=False)`；安全读 mock 调用为 `Overlay(download=False)` → `MMIO(2685403136,4096)` → `read(20)`，即基址 0xA0100000、范围 0x1000、偏移 0x14。返回的 0x504E5951 是 mock 预设值，绝非 PL 实际身份读回。适配骨架也只做相同的 ID mock 访问；CONTROL、STATUS、DATA 均未访问。

| 验证层 | 状态 | 限制 |
|---|---|---|
| 离线样本/契约 | PASS | 非 bitstream 有效性验证；非 HWH 设计语义验证 |
| Skill 隔离脚本 | PASS，19 项 | 主机软件与 mock |
| 本项目草稿/接口 mock | PASS | 仅身份适配边界；未实现采集协议 |
| 完整应用软件 | NOT_RUN | 温度编码、启动停止及 FIFO 协议待补 |
| RTL | NOT_RUN | 未提供 RTL/仿真 |
| 综合实现/时序/DRC | NOT_RUN | 未提供工程、约束、报告 |
| PYNQ/Overlay 实际元数据 | NOT_RUN | 无目标解释器且合成 XML 非真实 HWH |
| Overlay 实际下载/独立 ID | NOT_RUN | 无实板；合成文件不可加载 |
| 真实传感器/完整记录链路 | NOT_RUN | 外设、速率、精度、漏样、恢复未测 |
| 新板迁移 | NOT_RUN | UltraScale+ 类别和替代布局 mock 不能代替新板验证 |

## 接入计划

1. 沿用当前 Linux，先在现场记录精确板卡/器件、PYNQ 与 Python 版本和实际解释器、连接方式/权限、当前 Overlay 与入口。保留旧应用独立目录；完整 Overlay 替换当前 PL，其所有者需明确。
2. 获取当前设计的 RTL、BD/Tcl、XDC、手册、PS 配置、源版本与工具版本。核对 UltraScale+ 对应 PS/DDR/MIO、采样时钟复位、AXI 时钟/复位和外设电气；每个数字与协议补充来源。
3. 明确 CONTROL 命令编码、启动/停止/ACK/复位和超时；STATUS 位域及读清时机；FIFO 深度、empty 判断、DATA 位宽/样本编码、时间戳和溢出策略。明确温度单位、符号、比例、校准来源。缺少这些信息前不写采集驱动或试读 STATUS/DATA。
4. PL 负责稳定采样时序、传感器握手、FIFO 和必要时间戳；PS 负责配置调度、显式 FIFO 消费、校准/记录、异常和文件生命周期；上位机按需求可选。事件保序，溢出/失联应形成明确可记录的错误行为。
5. 测量采样率与每样本字节数，计算持续/峰值字节率、单次寄存器读取成本、消费调度延迟和 FIFO 可承受间隔，再决定寄存器读取是否满足需求。当前没有速率、深度和性能数据，不判定 DMA 必需或不必需。若需 DMA，再定义 Stream/TLAST、长度/对齐/DRE、内存端口、缓存同步、完成/超时/停止与缓冲释放所有权。
6. 按精确新板重建真实产物，同次导出 bit/hwh 并附源提交、工具、构建记录和哈希；替换本 fixture 的合成契约输入前，重新核对真实 HWH 的 IP 名称/VLNV/地址、寄存器与约束，不能把当前样本地址默认为新硬件不变。
7. 真实产物先离线检查；在已确认目标 PYNQ 解释器中默认运行元数据探针。完成板级核对和实际加载步骤后，仅选择无副作用 ID 读回。元数据/is_loaded 不是独立 PL 身份；总线失钟可能阻塞，Python 超时不能确保中断，应先核对时钟复位/互联。
8. 完成 mock、RTL 和实现报告后分层板测：ID → 明确控制协议 → 可控样本 FIFO → 真实传感器 → 持续记录。分别记录温度精度/校准、样本连续性、溢出、峰值速率、记录延迟、启动/停止/重启/断电恢复、故障与资源释放，按实际层次判定通过。

## 可用性问题与建议

1. `check_overlay.py` 有意只做 `ET.parse` XML 语法检查，不提取真实 HWH IP 布局，也不验证 .bit 头或目标器件。本合成文本 bit 与 SYNTHETIC_BOARD XML 因此得到 PASS。脚本 scope 与 board_test 已清楚限定，但使用者仍需注意“PASS”仅是该离线项目；建议输出显式的 `hwh_semantic_validation=NOT_RUN` / `bitstream_validation=NOT_RUN`，降低误读概率。
2. SKILL 工具入口末尾概括“JSON 输出”，但 `test_helpers.py` 实际是 unittest 格式，结果写 stderr。本次通过日志收集兼容，无阻塞；建议将该说明限定为 check/probe 两个 CLI。
3. 主契约 schema 仅覆盖 IP 与安全读，无法直接表达 CONTROL 命令、读清 STATUS、FIFO POP、时钟复位和未定协议。Skill 正文说明了这些要求，因此我另建 `interface-registers.json`；可增加完整接口附录模板以减少首次整理成本。
4. 精确目标板与解释器未知没有阻塞离线工作。用明确的未确认目标标签和 NOT_RUN 记录保持证据边界；不需要猜板卡、账号、PS7 或额外寄存器地址。

未修改 Skill，未修改其他用户文件，未访问任何真实硬件。本次结果不能证明第二种真实板卡已适配。


## 维护者处理记录

评估后已修正：两支检查工具输出JSON，unittest输出文本；离线输出新增bitstream_validation=NOT_RUN及hwh_design_semantics=NOT_RUN；新增完整接口附录模板表达初始化、写副作用、FIFO、时钟复位与缓冲协议。独立试用中的真实板端和换板结果仍为NOT_RUN。上文为试用时的观察，路径仅记录当时环境，不是工具运行默认值。
