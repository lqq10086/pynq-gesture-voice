##############################################################################
# 从纯文本源重建 Vivado 工程，不依赖任何已有的 xpr。
# 命令：python tools/run_tcl.py vivado/create_hardware_project.tcl \
#         --args "-part xc7z020clg400-2 -project_dir <工程目录> -rtl_dir <RTL 目录> [-xdc <文件>] [-bd <bd 脚本>] [-top <顶层名>]"
# 直接调用：vivado -mode batch -source create_hardware_project.tcl -tclargs ...
# 依赖：Vivado 2020.x 及以上；命令在 2023.2 上实测通过。
# 缺参数时不会硬报错，而是打印 MISSING_INPUT 并以 2 退出，交由调用方补齐。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv
::portable::tool_info [::portable::get expect_version ""]

set part        [::portable::get part ""]
set project_dir [::portable::get project_dir ""]
set project_name [::portable::get project_name [file tail $project_dir]]
set rtl_dir     [::portable::get rtl_dir ""]
set xdc_file    [::portable::get xdc ""]
set bd_script   [::portable::get bd ""]
set top_name    [::portable::get top ""]
set flatten     [::portable::get flatten rebuilt]

if {$part eq ""} { ::portable::need "part" "-part <device part, e.g. xc7z020clg400-2>" }
if {$project_dir eq ""} { ::portable::need "project_dir" "-project_dir <output dir for the Vivado project>" }
if {$rtl_dir ne ""} {
    ::portable::require_dir "rtl_dir" $rtl_dir
    set rtl_files [concat [glob -nocomplain [file join $rtl_dir *.v]] \
                         [glob -nocomplain [file join $rtl_dir *.sv]]]
    if {[llength $rtl_files] == 0} {
        ::portable::need "RTL sources" "no .v/.sv file under $rtl_dir"
    }
    set headers [concat [glob -nocomplain [file join $rtl_dir *.vh]] \
                         [glob -nocomplain [file join $rtl_dir *.svh]]]
    set mems [glob -nocomplain [file join $rtl_dir *.mem]]
} else {
    ::portable::need "rtl_dir" "-rtl_dir <dir with .v/.sv sources; may be empty when creating the project first>"
}
if {$xdc_file ne ""} { ::portable::require_file "xdc_file" $xdc_file }
if {$bd_script ne ""} { ::portable::require_file "bd_script" $bd_script }
::portable::require_inputs

::portable::summary [list part $part project_dir $project_dir project_name $project_name \
                          rtl_dir $rtl_dir xdc $xdc_file bd $bd_script top $top_name]

create_project -force $project_name $project_dir -part $part
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property default_lib xil_defaultlib [current_project]

if {[info exists rtl_files] && [llength $rtl_files]} {
    add_files -norecurse $rtl_files
}
if {[info exists headers] && [llength $headers]} {
    add_files -norecurse $headers
    set_property file_type {Verilog Header} [get_files -quiet [list {*}[lmap h $headers {file tail $h}]]]
}
if {[info exists mems] && [llength $mems]} {
    add_files -norecurse $mems
    set_property file_type {Memory Initialization Files} \
        [get_files -quiet [list {*}[lmap m $mems {file tail $m}]]]
}
if {$rtl_dir ne ""} {
    set_property include_dirs [list $rtl_dir] [get_filesets sources_1]
}
if {$xdc_file ne ""} {
    add_files -fileset constrs_1 -norecurse $xdc_file
}

if {$bd_script ne ""} {
    source $bd_script
    set bd_files [get_files -quiet *.bd]
    if {[llength $bd_files] == 0} {
        ::portable::need "bd_script" "$bd_script ran but produced no .bd file"
        ::portable::require_inputs
    }
    generate_target all $bd_files
    make_wrapper -files $bd_files -top
    foreach wrapper [get_files -quiet *system_wrapper.v] {
        add_files -norecurse $wrapper
    }
}

if {$top_name ne ""} {
    set_property top $top_name [get_filesets sources_1]
}
update_compile_order -fileset sources_1
if {[llength [get_runs synth_1]]} {
    set_property STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY $flatten [get_runs synth_1]
}

puts "PROJECT_CREATED=[file join $project_dir ${project_name}.xpr]"
puts "NEXT: python tools/run_tcl.py vivado/build_hardware.tcl --args \"-project_dir $project_dir -project_name $project_name\""
