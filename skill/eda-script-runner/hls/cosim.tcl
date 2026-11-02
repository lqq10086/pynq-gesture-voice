##############################################################################
# 对已 csynth 的 solution 跑 C/RTL 协同仿真。
# 命令：python tools/run_tcl.py hls/cosim.tcl --args "-project <HLS 工程目录> [-solution solution1]"
# 直接调用：vitis_hls -f cosim.tcl -tclargs ...
# 版本依赖：cosim_design 的 -rtl/-tool/-trace_level 在 2020.1 到 2025.2 间写法未变。
# 判据：cosim 通过只说明 C 与 RTL 在给定激励下一致，不说明布线后能收敛；
#       协同仿真报 "Cannot find solution" 说明还没 csynth，先跑 build_ip.tcl。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

set project_name [::portable::get project ""]
set solution     [::portable::get solution solution1]
set tool         [::portable::get tool xsim]
set trace_level  [::portable::get trace_level none]
set rtl_lang     [::portable::get rtl verilog]

::portable::need "project_dir" "-project <HLS project dir>"
if {$project_name ne "" && ![file isdirectory $project_name]} {
    ::portable::need "HLS project" "$project_name does not exist"
}
if {$project_name ne "" && ![file isdirectory [file join $project_name $solution]]} {
    ::portable::need "solution" "no solution $solution under $project_name; run build_ip.tcl (csynth) first"
}
::portable::require_inputs
::portable::summary [list project $project_name solution $solution tool $tool rtl $rtl_lang]

open_project $project_name
open_solution $solution
cosim_design -rtl $rtl_lang -tool $tool -trace_level $trace_level
puts "COSIM_DONE=$project_name/$solution"
exit
