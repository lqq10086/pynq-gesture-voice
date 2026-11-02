# 用途：时钟约束骨架。所有时钟都必须在这里定义，未定义的时钟不会出现在时序分析里。
# 输入：主时钟引脚与频率。
# 输出：create_clock 与派生时钟。
# 工具版本：Vivado 2020.1 及以上。
# 未验证项：未包含 IO delay，输入输出接口需另写。

# 主时钟：把周期改成你板上的实际值，不要用默认猜测
create_clock -period 10.000 -name clk_main [get_ports clk]

# 由 PL 产生的时钟必须声明，否则它的路径不会被分析
# create_generated_clock -name clk_pixel -source [get_pins clk_wiz/inst/clk_in1] \
#     -divide_by 2 [get_pins clk_wiz/inst/clk_out1]

# 异步时钟域之间不要用 false_path 掩盖，先确认有同步器再写 clock_groups
# set_clock_groups -asynchronous -group [get_clocks clk_main] -group [get_clocks clk_pixel]
