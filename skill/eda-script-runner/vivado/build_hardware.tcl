##############################################################################
# 一次跑完综合、实现、比特流、报告与 xsa，并在时序或 DRC 不达标时主动失败。
#
# 用法（推荐，由包装脚本负责找工具、记日志、判退出码）：
#   python tools/run_tcl.py vivado/build_hardware.tcl --cwd <一个空目录> \
#       --args "-project_dir <含 .xpr 的目录> -jobs 4"
# 直接调用：
#   vivado -mode batch -notrace -source build_hardware.tcl -tclargs -project_dir <目录>
#
# 参数（缺省值只是某个工程的取值，不是前提）：
#   -project_dir   必填，含 <name>.xpr 的目录
#   -project_name  缺省从目录里的 .xpr 推
#   -reports_dir   缺省 <project_dir>/../reports
#   -bit_dir       缺省 <project_dir>/../bit
#   -xsa_dir       缺省 <project_dir>/../xsa
#   -jobs          缺省 6
#   -extra_srcs    可选，额外加入源文件集的 RTL 文件列表（空格分隔）
#
# 依赖：Vivado 2020.x 及以上，已在 2023.2 上跑通一个最小 LED 工程。
# 退出：0 成功；2 缺输入或门禁失败（GATE_FAIL 前缀）；3 工具版本不符。
# 判据：负裕量、阻塞 DRC、综合/实现未完成都算失败，这是本脚本的目的，不要改成只告警。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

set project_dir [::portable::get project_dir ""]
if {$project_dir eq ""} {
    ::portable::need {project_dir} "directory that contains <name>.xpr"
}
set project_dir [file normalize $project_dir]

set project_name [::portable::get project_name ""]
if {$project_name eq ""} {
    set xprs [glob -nocomplain [file join $project_dir *.xpr]]
    if {[llength $xprs] == 0} {
        ::portable::need {project_name} "no .xpr in that directory; pass -project_name or run create_hardware_project.tcl first"
    }
    set project_name [file rootname [file tail [lindex $xprs 0]]]
}

# 产物默认落在工程目录的上一级。不要把默认值指向脚本所在目录，否则会写进技能包。
set out_root    [file normalize [file join $project_dir ..]]
set reports_dir [file normalize [::portable::get reports_dir [file join $out_root reports]]]
set bit_dir     [file normalize [::portable::get bit_dir     [file join $out_root bit]]]
set xsa_dir     [file normalize [::portable::get xsa_dir     [file join $out_root xsa]]]
set xpr [file join $project_dir ${project_name}.xpr]
::portable::require_file "project file xpr" $xpr
::portable::require_inputs

set build_jobs [::portable::get jobs 6]
if {![string is integer -strict $build_jobs] || $build_jobs < 1} {
    error "GATE_FAIL args: -jobs must be a positive integer, got '$build_jobs'"
}
::portable::summary [list project $xpr reports_dir $reports_dir bit_dir $bit_dir xsa_dir $xsa_dir jobs $build_jobs]

file mkdir $reports_dir
file mkdir $bit_dir
file mkdir $xsa_dir
open_project $xpr

# 原工程专有的白平衡源码是可选的：给了 -extra_srcs 才加入，否则跳过。
# 换工程时这些文件不存在是正常的，不能因为「文件不在」把整次构建判失败。
set extra_srcs [split [::portable::get extra_srcs ""]]
foreach src $extra_srcs {
    if {![file exists $src]} {
        puts "SKIPPED (extra_srcs entry not found): $src"
        continue
    }
    if {[llength [get_files -quiet [file tail $src]]] == 0} {
        add_files -norecurse $src
        puts "ADDED_EXTRA_SRC=$src"
    }
}
update_compile_order -fileset sources_1

reset_run synth_1
launch_runs synth_1 -jobs $build_jobs
wait_on_run synth_1
if {[get_property STATUS [get_runs synth_1]] ne "synth_design Complete!"} {
    error "GATE_FAIL synthesis: [get_property STATUS [get_runs synth_1]]"
}

launch_runs impl_1 -to_step write_bitstream -jobs $build_jobs
wait_on_run impl_1
if {[get_property STATUS [get_runs impl_1]] ne "write_bitstream Complete!"} {
    error "GATE_FAIL implementation: [get_property STATUS [get_runs impl_1]]"
}

open_run impl_1
# Two utilization reports on purpose: the default one carries the summary table
# (CLB LUTs / CLB Registers / Block RAM Tile / DSPs with Util%), which is what
# b03_collect_reports.tcl parses. The hierarchical one only has per-instance counts.
report_utilization -file [file join $reports_dir utilization.rpt]
report_utilization -hierarchical -file [file join $reports_dir utilization_hier.rpt]
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -max_paths 20 -file [file join $reports_dir timing_summary.rpt]
report_drc -file [file join $reports_dir drc.rpt]
report_route_status -file [file join $reports_dir route_status.rpt]
report_clock_interaction -delay_type min_max -file [file join $reports_dir clock_interaction.rpt]
report_cdc -details -file [file join $reports_dir cdc.rpt]
check_timing -verbose -file [file join $reports_dir check_timing.rpt]
report_bus_skew -file [file join $reports_dir bus_skew.rpt]

set failing_paths [get_timing_paths -quiet -slack_lesser_than 0.0 -max_paths 1]
if {[llength $failing_paths] != 0} {
    set worst_slack [get_property SLACK [lindex $failing_paths 0]]
    error "GATE_FAIL setup: negative-slack timing path: WNS=$worst_slack ns"
}
set failing_hold_paths [get_timing_paths -quiet -delay_type min -slack_lesser_than 0.0 -max_paths 1]
if {[llength $failing_hold_paths] != 0} {
    error "GATE_FAIL hold: negative hold slack"
}
set blocking_drc [get_drc_violations -quiet -filter {SEVERITY == "Error"}]
if {[llength $blocking_drc] != 0} {
    error "GATE_FAIL drc: $blocking_drc Error-level DRC violation(s)"
}

# 位流文件名跟着顶层走：不同工程的 .bit 名字不同，按 impl_1 的运行目录找
set impl_dir [get_property DIRECTORY [get_runs impl_1]]
set bit_candidates [glob -nocomplain [file join $impl_dir *.bit]]
if {[llength $bit_candidates] == 0} {
    error "GATE_FAIL bitstream: impl finished but no .bit under $impl_dir; check write_bitstream"
}
set impl_bit [lindex $bit_candidates 0]
file copy -force $impl_bit [file join $bit_dir system.bit]
puts "BITSTREAM_SOURCE=$impl_bit"

# 只有带 PS（BD 里有 processing_system7）的工程，xsa 才可能被 Vitis/PYNQ 用。
# 纯 RTL 工程导出的 xsa 里没有 .hwh，下游打开会失败；这一点写进报告，不要让人误以为可用。
set ps_cells [get_bd_cells -quiet -hier -filter {VLNV =~ "xilinx.com:ip:processing_system7*"}]
if {[llength $ps_cells] == 0} {
    puts "NOTE: this project has no PS7; the exported xsa has no handoff (.hwh) and cannot be used by Vitis/PYNQ directly"
}
write_hw_platform -fixed -include_bit -force -file [file join $xsa_dir system.xsa]

# 地址表：有 BD 就按真实地址段写，没有就写一句说明。
# 旧版本在这里写死了原工程的 VDMA/帧缓冲地址，换个工程会变成伪造数据，已删除。
set addr_file [open [file join $reports_dir address_map.txt] w]
puts $addr_file "Vivado [version -short] / part [get_property PART [current_project]]"
if {[llength [get_files -quiet *.bd]] == 0} {
    puts $addr_file "NO_BD: no block design in this project, no address segments to report"
} else {
    foreach seg [get_bd_addr_segs -quiet] {
        set offset [get_property OFFSET $seg]
        set range  [get_property RANGE  $seg]
        if {$offset ne "" && $range ne ""} {
            puts $addr_file "$seg | 0x[format %08X $offset]-0x[format %08X [expr {$offset + $range - 1}]]"
        }
    }
}
close $addr_file

set ip_file [open [file join $reports_dir ip_versions.txt] w]
puts $ip_file "Vivado=[version -short]"
foreach ip [lsort [get_ips]] {
    puts $ip_file "[get_property NAME $ip] | [get_property IPDEF $ip]"
}
close $ip_file

report_ip_status -file [file join $reports_dir ip_status.txt]

puts "BITSTREAM=[file join $bit_dir system.bit]"
puts "XSA=[file join $xsa_dir system.xsa]"
puts "REPORTS_DIR=$reports_dir"
close_project
exit 0
