##############################################################################
# 打开已有 DCP，只读导出时序路径、时钟与裕量，不写回工程、不改设计。
# 命令：python tools/run_tcl.py vivado/report_timing_paths.tcl \
#         --args "-dcp <routed.dcp> [-out_dir <报告目录>] [-from_pattern <端口匹配>] [-to_pattern <单元匹配>]"
# 直接调用：vivado -mode batch -source report_timing_paths.tcl -tclargs <报告目录>
# 版本依赖：open_checkpoint/report_timing/get_timing_paths/get_clocks 的选项在 2020.x 到 2025.x 未变，
#           本包内可移植性最高的一支：只读、不依赖工程与 IP 版本。
# 与原脚本的区别：端口与单元匹配从写死的 DVP 十个脚改成可传 -from_pattern/-to_pattern，
#                 数量不符时打印 INCOMPLETE 并把实际数量写进结果，不再因 "不是 10 个" 直接报错退出。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

# 兼容老用法：唯一参数是报告目录
if {[llength $argv] == 1 && [string index [lindex $argv 0] 0] ne "-"} {
    set ::portable::opt(out_dir) [lindex $argv 0]
}
set dcp_path [::portable::get dcp ""]
set out_dir  [::portable::get out_dir ""]
if {$out_dir eq "" && $dcp_path ne ""} { set out_dir [file dirname $dcp_path] }
if {$dcp_path eq "" && $out_dir ne ""} { set dcp_path [file join $out_dir final_routed.dcp] }
set from_pattern [::portable::get from_pattern {cam_data[*] cam_href cam_vsync}]
set to_pattern   [::portable::get to_pattern {*camera_axis_i/camera_data_reg* *camera_axis_i/camera_href_reg* *camera_axis_i/camera_vsync_reg*}]
set clock_name   [::portable::get clock cam_pclk]
set max_paths    [::portable::get max_paths 20]

::portable::require_file "routed dcp" $dcp_path
::portable::require_inputs
file mkdir $out_dir
::portable::summary [list dcp $dcp_path out_dir $out_dir from $from_pattern to $to_pattern clock $clock_name]

open_checkpoint $dcp_path
set from_ports [get_ports -quiet $from_pattern]
set to_cells   [get_cells -quiet -hier -filter "NAME =~ *camera_axis_i/*"]
if {[llength $to_cells] == 0} {
    # 换工程后单元名当然不同：退化为按 -to_pattern 逐个匹配
    set to_cells [concat {*}[lmap p $to_pattern {get_cells -quiet -hier -filter "NAME =~ $p"}]]
}
set from_count [llength $from_ports]
set to_count   [llength $to_cells]
puts "SOURCE_PORTS=$from_count TARGET_CELLS=$to_count"
if {$from_count == 0} {
    puts "INCOMPLETE: no matching source port, check -from_pattern; if the design has no source-synchronous input, this script does not apply"
}
set facts [open [file join $out_dir timing_facts.tsv] w]
puts $facts "from_count\t$from_count"
puts $facts "to_count\t$to_count"

if {$from_count > 0 && $to_count > 0} {
    report_timing -from $from_ports -to $to_cells -delay_type max -max_paths $max_paths -nworst 2 \
        -path_type full_clock_expanded -file [file join $out_dir setup_paths.rpt]
    report_timing -from $from_ports -to $to_cells -delay_type min -max_paths $max_paths -nworst 2 \
        -path_type full_clock_expanded -file [file join $out_dir hold_paths.rpt]
    foreach port $from_ports {
        foreach type {max min} {
            set paths [get_timing_paths -quiet -from $port -to $to_cells -delay_type $type -max_paths 1]
            if {[llength $paths] != 1} {
                puts $facts "$port\t$type\tUNCONSTRAINED"
            } else {
                puts $facts "$port\t$type\t[get_property SLACK $paths]"
            }
        }
    }
}
foreach c [get_clocks -quiet -include_generated_clocks $clock_name] {
    puts $facts "clock:$c\t[get_property PERIOD $c]\t[get_property WAVEFORM $c]"
}
close $facts
puts "FACTS=[file join $out_dir timing_facts.tsv]"
close_design
