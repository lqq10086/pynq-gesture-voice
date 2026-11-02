# 契约与访问

复制 assets 中的 JSON 到项目，schema_version=1。文件路径相对 JSON，禁止绝对路径、路径逃逸与指向目录外的符号链接。bit/hwh 同目录同主干；哈希由本次构建或可信清单提供，null 必须补齐。脚本不会自动把当前文件当作正确版本批准。

ips 记录当前 HWH 的完整名称、type/VLNV、phys_addr、addr_range、evidence。层次名称按 HWH 键填写。安全读仅支持对齐的 32 位访问；每项含 name、offset、mask、expected、side_effect=none、evidence。整数或 0x 字符串均可，布尔不是整数。FIFO、读清状态或先写才能安全读的地址不能列入。空 safe_reads 表示仅校验元数据，没有独立 PL 身份核验。

--load 下载 bit，可能应用 HWH 时钟设置；此前应按目标板核对 PS preset 与外设电气。--read 仅在 is_loaded 为真且全部元数据匹配后执行。默认不做 MMIO。脚本不猜测初始化写入。

## MMIO 范式

```python
from pynq import Overlay, MMIO
ol = Overlay(str(bit_path), download=load_requested)
if not ol.is_loaded():
    raise RuntimeError("Expected overlay is not loaded")
info = ol.ip_dict[ip_name]
assert info["phys_addr"] == expected_base
assert info["addr_range"] == expected_range
assert info["type"] == expected_vlnv
mmio = MMIO(info["phys_addr"], info["addr_range"])
value = mmio.read(verified_safe_offset)
```

变量均来自当前契约，写操作由业务驱动按 RTL 实现。

## register_map 范式

HWH 有寄存器描述、驱动支持时，可按已核对的名称访问选定寄存器。顶层 IP 用 getattr；层次 IP 按对象树逐级取属性，核对所用版本的命名映射。

```python
ip = getattr(ol, top_level_ip_name)
register = getattr(ip.register_map, verified_register_name)
value = int(register)  # 实际读取，必须先确认没有副作用
```

print(ip.register_map) 可能读多项寄存器，不是纯元数据打印。字段赋值也可能实际写硬件；W1C、命令位和硬件动态位不能套用通用读改写。无寄存器描述时改用已核对的 MMIO 偏移，不伪造字段名。

is_loaded 与 HWH 不能发现所有外部重配置/过期元数据情形，可信独立 ID 读回才能进一步核实 PL。失钟等问题可能令底层总线读取阻塞，Python 超时不能保证中断；先查时钟/复位/互联，不把探针作为任意地址扫描器。

## 官方依据

- [Overlay](https://pynq.readthedocs.io/en/v3.0.0/pynq_libraries/overlay.html)：加载和元数据。
- [Python Overlay API](https://pynq.readthedocs.io/en/v3.0.0/overlay_design_methodology/python_overlay_api.html)：驱动和层次封装。
- [MMIO](https://pynq.readthedocs.io/en/v3.0.0/pynq_libraries/mmio.html)：少量寄存器交互。
- [Allocate](https://pynq.readthedocs.io/en/v3.0.0/pynq_libraries/allocate.html)：缓冲与缓存同步。

访问日期 2026-11-01；文档解释 API，不表示本包已实测全部版本与后端。
