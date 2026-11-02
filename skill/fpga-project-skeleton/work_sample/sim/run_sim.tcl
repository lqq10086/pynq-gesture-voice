# 用途：用 Vivado 自带仿真器跑一次测试平台，留下日志与波形。
# 用法（推荐由 eda-script-runner 的包装脚本调用，它会找工具并落日志）：
#   python tools/run_tcl.py sim/run_sim.tcl --cwd <空目录> \
#       --args "-proj_dir <工程目录> -top <测试平台模块> -tb <测试平台文件> -part <器件>"
# 参数（都有缺省值，但缺省值只是某个工程的取值，换工程要覆盖）：
#   -proj_dir  缺省为本脚本的上两级目录
#   -top       缺省 tb_template
#   -tb        缺省 <proj_dir>/sim/tb/tb_template.v
#   -rtl       可选，RTL 文件列表（空格分隔）
#   -part      缺省 xc7z020clg400-2；必须与位流同器件，否则 IP 仿真库对不上
#   -run_ns    仿真时长（纳秒），缺省 1000
#   -wave      1 记录波形（默认 1）
# 退出码：0 跑完；2 缺输入或仿真报错；3 工具版本不符。
# 未验证项：波形格式固定 wdb；Vivado 2019.x 的 xsim 参数略有差异，未逐一验证。

source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

set here     [file dirname [file normalize [info script]]]
set proj_dir [file normalize [::portable::get proj_dir [file join $here ..]]]
set top      [::portable::get top tb_template]
set tb_file  [file normalize [::portable::get tb [file join $proj_dir sim tb tb_template.v]]]
set part     [::portable::get part xc7z020clg400-2]
set run_ns   [::portable::get run_ns 1000]
set wave     [::portable::get wave 1]
set rtl_files [split [::portable::get rtl ""]]

::portable::require_dir "proj_dir" $proj_dir
::portable::require_file "tb_file" $tb_file
::portable::require_inputs

set work_dir [file join $proj_dir sim work]
file mkdir $work_dir
set log_file [file join $work_dir run_sim.log]

# 参数先落盘：事后要能说明这次到底跑了哪个顶层、哪个器件
set fp [open $log_file w]
puts $fp "TOP=$top"
puts $fp "TB=$tb_file"
puts $fp "PART=$part"
puts $fp "RUN_NS=$run_ns"
puts $fp "WAVE=$wave"
close $fp

puts "SIM_TOP=$top TB=$tb_file PART=$part RUN_NS=$run_ns"
if {[catch {
    if {[llength $rtl_files]} {
        xvlog -sv --incr --log [file join $work_dir xvlog.log] {*}$rtl_files
    }
    xvlog -sv --incr --log [file join $work_dir xvlog.log] $tb_file
    xelab -debug typical --incr --log [file join $work_dir xelab.log] $top -s ${top}_sim
    if {$wave == 1} {
        log_wave -recursive *
    }
    run [expr {$run_ns}]ns
} err]} {
    puts "ERROR: simulation failed: $err"
    puts "ERROR: logs under $work_dir"
    exit 2
}

puts "SIM_DONE wave=$wave log=$log_file"
exit 0
