##############################################################################
# HLS C 仿真 + 综合 + 导出 IP（Vitis HLS 2020.1 及以上，Vivado HLS 2019.x 需去掉 -flow_target）。
# 命令：python tools/run_tcl.py hls/build_ip.tcl \
#         --args "-project <HLS 工程目录> -top <顶层函数> -src <cpp> -tb <tb.cpp> -vectors_dir <向量目录> -part xc7z020clg400-2"
# 直接调用：vitis_hls -f build_ip.tcl -tclargs ...
# 版本依赖：open_project/open_solution/set_top/csynth_design/export_design 在 2020.1 到 2025.2 写法一致；
#           -flow_target 从 2020.1 才有，更老的 Vivado HLS 会把它当未知参数。
# 缺参数时打印 MISSING_INPUT 并以 2 退出，不猜路径、不动源码。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

set project_name [::portable::get project ""]
set top_name     [::portable::get top ""]
set src_file     [::portable::get src ""]
set tb_file      [::portable::get tb ""]
set vectors_dir  [::portable::get vectors_dir ""]
set solution     [::portable::get solution solution1]
set part         [::portable::get part ""]
set period       [::portable::get period 10]
set flow_target  [::portable::get flow_target vivado]
set m_axi_addr64 [::portable::get m_axi_addr64 0]

::portable::need "project_dir" "-project <HLS project dir>"
::portable::need "top" "-top <top function name>"
if {$part eq ""} { ::portable::need "part" "-part <device part, e.g. xc7z020clg400-2>" }
if {$src_file ne ""} { ::portable::require_file "src_file" $src_file }
if {$tb_file ne ""}  { ::portable::require_file "testbench" $tb_file }
if {$vectors_dir ne ""} {
    if {[::portable::require_dir "vectors_dir" $vectors_dir]} {
        foreach name {input_q weight_q bias_q mult acc_ref act_ref} {
            set f [file join $vectors_dir $name.bin]
            if {![file exists $f]} { ::portable::need "test vector" $f }
        }
    }
}
if {$project_name ne "" && ![file isdirectory $project_name]} {
    ::portable::need "HLS project" "$project_name is not an HLS project; run build_ip.tcl first or pass -project"
}
::portable::require_inputs
::portable::summary [list project $project_name top $top_name solution $solution \
                          part $part period $period flow_target $flow_target]

open_project $project_name
set_top $top_name
if {$src_file ne ""} { add_files $src_file }
if {$tb_file ne ""} {
    add_files -tb $tb_file
    if {$vectors_dir ne ""} {
        foreach name {input_q weight_q bias_q mult acc_ref act_ref} {
            add_files -tb [file join $vectors_dir $name.bin]
        }
    }
}
if {[file isdirectory [file join $project_name $solution]]} {
    open_solution $solution
} else {
    open_solution $solution -flow_target $flow_target
}
set_part $part
create_clock -period $period -name default
config_interface -m_axi_addr64=$m_axi_addr64
csim_design
csynth_design
export_design -format ip_catalog -rtl verilog
puts "HLS_IP_EXPORTED=[file join $project_name $solution impl ip]"
exit
