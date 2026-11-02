# 用途：为一组源文件建 Vivado 工程，综合、实现、写比特流，并导出时序与资源报告。
# 输入：工程目录、工程名、器件、顶层、RTL 与 XDC 清单；用 -tclargs 传入，缺省值可从工程骨架推出来。
# 输出：<工程目录>/build/out/<工程名>/<run>/ 下的 bit、hwh、timing_summary.rpt、utilization.rpt、cdc.rpt、drc.rpt
# 退出码：0 布线后时序为正；2 构建完成但时序为负；3 缺输入或环境不对。
# 工具版本：Vivado 2020.1 及以上；2023.2 上实跑通过，其他版本未逐一验证。
# 失效条件：工程里用了 BD（IP Integrator）或 AI Engine 时，本脚本只做综合实现，不生成 IP 相关文件。

# ---------------------------------------------------------------------------
# 参数解析（不依赖外部库，缺参数时打印清楚缺什么）
# ---------------------------------------------------------------------------
set here [file dirname [file normalize [info script]]]
set root [file normalize [file join $here .. ..]]

set proj_dir  [file join $root build out my_project]
set proj_name "my_project"
set part_name ""
set top_module "top_template"
set run_name  "v001"
set jobs 8
set rtl_files {}
set xdc_files {}

for {set i 0} {$i < [llength $argv]} {incr i} {
    set key [lindex $argv $i]
    switch -- $key {
        -proj_dir  { set proj_dir  [lindex $argv [incr i]] }
        -proj_name { set proj_name [lindex $argv [incr i]] }
        -part      { set part_name [lindex $argv [incr i]] }
        -top       { set top_module [lindex $argv [incr i]] }
        -run       { set run_name  [lindex $argv [incr i]] }
        -jobs      { set jobs      [lindex $argv [incr i]] }
        -rtl       { set rtl_files [lindex $argv [incr i]] }
        -xdc       { set xdc_files [lindex $argv [incr i]] }
        default {
            puts "ERROR: unknown argument $key"
            puts "usage: -proj_dir <dir> -proj_name <name> -part <part> -top <top> -rtl <files> -xdc <files>"
            exit 3
        }
    }
}

proc fail {msg} {
    puts "ERROR: $msg"
    exit 3
}

if {[llength [info commands create_project]] == 0} {
    fail "not running inside Vivado batch mode; start it via run_tcl.py or vivado -mode batch"
}

# 缺省：用骨架里的模板文件，而不是猜名字
if {[llength $rtl_files] == 0} {
    foreach cand [list [file join $root src rtl "${top_module}.v"] \
                       [file join $root src rtl top_template.v]] {
        if {[file exists $cand]} { set rtl_files [list $cand]; break }
    }
}
if {[llength $xdc_files] == 0} {
    foreach cand [list [file join $root src constraints clocks_template.xdc] \
                       [file join $root src constraints pins_template.xdc]] {
        if {[file exists $cand]} { lappend xdc_files $cand }
    }
}

set missing {}
if {[llength $rtl_files] == 0} { lappend missing "RTL sources (-rtl, or src/rtl/top_template.v)" }
if {$part_name eq ""}         { lappend missing "device part (-part, e.g. xc7z020clg400-2)" }
if {$top_module eq ""}        { lappend missing "top module name (-top)" }
foreach f [concat $rtl_files $xdc_files] {
    if {![file exists $f]} { lappend missing "file not found: $f" }
}
if {[llength $missing] > 0} {
    foreach m $missing { puts "MISSING_INPUT: $m" }
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    exit 3
}

puts "INFO: vivado [version -short], part $part_name, top $top_module, jobs $jobs"

set out_dir  [file normalize [file join $proj_dir build out $proj_name $run_name]]
set proj_file [file join $proj_dir "${proj_name}.xpr"]
file mkdir $out_dir

# ---------------------------------------------------------------------------
# 建工程：已存在则复用
# ---------------------------------------------------------------------------
if {[file exists $proj_file]} {
    open_project $proj_file
    puts "INFO: reusing existing project $proj_file"
} else {
    create_project -force $proj_name $proj_dir -part $part_name
    set_property target_language Verilog [current_project]
    foreach f $rtl_files { add_files -norecurse -fileset sources_1 [file normalize $f] }
    foreach f $xdc_files { add_files -norecurse -fileset constrs_1 [file normalize $f] }
    set_property top $top_module [current_fileset]
    puts "INFO: creating project $proj_file"
}
update_compile_order -fileset sources_1

# ---------------------------------------------------------------------------
# 综合与实现：每一步都检查，不要等最后
# ---------------------------------------------------------------------------
reset_run synth_1
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} {
    puts "ERROR: synthesis failed, see [get_property DIRECTORY [get_runs synth_1]]"
    exit 2
}

reset_run impl_1
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} {
    puts "ERROR: implementation failed, see [get_property DIRECTORY [get_runs impl_1]]"
    exit 2
}

# ---------------------------------------------------------------------------
# 报告与产物
# ---------------------------------------------------------------------------
set impl_dir [get_property DIRECTORY [get_runs impl_1]]
open_run impl_1
report_timing_summary -delay_type min_max -max_paths 10 -file [file join $out_dir timing_summary.rpt]
report_utilization             -file [file join $out_dir utilization.rpt]
report_cdc -quiet              -file [file join $out_dir cdc.rpt]
report_drc -quiet              -file [file join $out_dir drc.rpt]

set bit_src [lindex [glob -nocomplain [file join $impl_dir *.bit]] 0]
if {$bit_src eq ""} { puts "ERROR: no bitstream generated"; exit 2 }
file copy -force $bit_src [file join $out_dir "${proj_name}.bit"]

set hwh_src [lindex [glob -nocomplain [file join $impl_dir *.hwh]] 0]
if {$hwh_src ne ""} {
    file copy -force $hwh_src [file join $out_dir "${proj_name}.hwh"]
} else {
    puts "WARN: no HWH (normal for pure RTL); if PYNQ needs a register map, supply the HWH or XSA separately"
}

# ---------------------------------------------------------------------------
# 只读时序汇总：找 WNS(ns) 表头，再取紧随其后的数值行
# Vivado 的报告里表头与数值不在同一行，用单行正则抓不到数。
# ---------------------------------------------------------------------------
proc read_summary_ns {path key} {
    set fp [open $path r]
    set text [read $fp]
    close $fp
    set lines [split $text "\n"]
    for {set i 0} {$i < [llength $lines]} {incr i} {
        if {[string first "${key}(ns)" [lindex $lines $i]] >= 0} {
            for {set j [expr {$i + 1}]} {$j < [llength $lines]} {incr j} {
                set row [string trim [lindex $lines $j]]
                if {[regexp {^(-?[0-9]+\.?[0-9]*)\s+(-?[0-9]+\.?[0-9]*)} $row -> a b]} {
                    return $a
                }
            }
        }
    }
    return "NA"
}

set wns [read_summary_ns [file join $out_dir timing_summary.rpt] "WNS"]
set whs [read_summary_ns [file join $out_dir timing_summary.rpt] "WHS"]
puts "RESULT: WNS=$wns WHS=$whs"
if {$wns eq "NA"} {
    puts "ERROR: cannot parse WNS from timing_summary.rpt; do not substitute an estimate"
    exit 2
}
if {$wns < 0} {
    puts "RESULT: BUILD_OK TIMING_NEGATIVE"
    exit 2
}
puts "RESULT: BUILD_OK TIMING_POSITIVE"
exit 0
