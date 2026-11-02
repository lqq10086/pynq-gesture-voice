# 用途：引脚约束骨架。引脚号必须来自板卡原理图，不要复制其他板卡的值。
# 输入：板卡型号。
# 输出：端口到封装的映射与 IO 标准。
# 工具版本：Vivado 2020.1 及以上。
# 未验证项：未包含 DDR 与高速收发器引脚，这类引脚由 IP 自动生成。

# set_property -dict {PACKAGE_PIN A1  IOSTANDARD LVCMOS33} [get_ports clk]
# set_property -dict {PACKAGE_PIN B1  IOSTANDARD LVCMOS33} [get_ports rst]

# 输入引脚若有时序要求，配合 set_input_delay 使用，单写引脚约束不够
