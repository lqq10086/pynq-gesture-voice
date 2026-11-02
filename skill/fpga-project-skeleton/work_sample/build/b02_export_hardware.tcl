# 用途：从已实现的工程导出 XSA，供 Vitis 或 PYNQ 侧使用。
# 输入：已完成的 impl_1 运行。
# 输出：out/<proj>/<run>/<proj>.xsa 与解包后的 HWH。
# 退出码：0 成功，2 实现未完成，3 参数缺失。
# 工具版本：Vivado 2020.1 及以上，export_hardware 命令在 2023.1 与 2025.2 上一致。
# 未验证项：未在启用 AI Engine 的工程上执行。

proc fail {msg} { puts "ERROR: $msg"; exit 3 }

if {[llength [info commands open_project]] == 0} { fail "not in Vivado batch mode" }

set proj_file [lindex $argv 0]
set out_xsa   [lindex $argv 1]
if {$proj_file eq "" || $out_xsa eq ""} { fail "usage: b02_export_hardware.tcl <xpr> <out.xsa>" }

open_project $proj_file
set impl [get_runs impl_1]
if {[get_property PROGRESS $impl] ne "100%"} {
    puts "ERROR: impl_1 not complete"
    exit 2
}
write_hw_platform -fixed -include_bit -force $out_xsa
puts "INFO: exported $out_xsa"
exit 0
