set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set project_dir [file join $root_dir hardware_build vivado_flash_pl01]
set project_name flash_pl01
set workspace_root $root_dir
set root_dir $workspace_root

if {[version -short] ne "2023.1"} {error "Vivado 2023.1 required"}
if {[file exists [file join $project_dir ${project_name}.xpr]]} {
 error "V3.2-384 project already exists; use mini_build.tcl to rebuild it"
} else {
 create_project $project_name $project_dir -part xc7z020clg400-2
}
if {![info exists ::pl01_hls_repo]} {error "Set ::pl01_hls_repo to the qualified exported IP"}
set_property ip_repo_paths [list [file normalize $::pl01_hls_repo]] [current_project]
update_ip_catalog
set_property XPM_LIBRARIES {XPM_CDC} [current_project]
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property default_lib xil_defaultlib [current_project]

set rtl_files [glob -nocomplain [file join $root_dir hardware rtl *.v]]
add_files -norecurse $rtl_files
set rtl_header [file join $root_dir hardware rtl ov5640_color_delta_count.vh]
set memory_files [list \
    [file join $root_dir hardware rtl ov5640_isp_baseline.mem] \
    [file join $root_dir hardware rtl af_firmware.mem] \
    [file join $root_dir hardware rtl ov5640_vga_rgb565.mem] \
    [file join $root_dir hardware rtl ov5640_720p_rgb565.mem] \
    [file join $root_dir hardware rtl ov5640_color_delta.mem] \
    [file join $root_dir hardware rtl fpga_color_gamma_r.mem] \
    [file join $root_dir hardware rtl fpga_color_gamma_g.mem] \
    [file join $root_dir hardware rtl fpga_color_gamma_b.mem] \
    [file join $root_dir hardware rtl fpga_spatial_gain_r.mem] \
    [file join $root_dir hardware rtl fpga_spatial_gain_b.mem]]
set rtl_headers [list \
    [file join $root_dir hardware rtl ov5640_isp_baseline_count.vh] \
    $rtl_header \
    [file join $root_dir hardware rtl fpga_color_profile.vh]]
add_files -norecurse $rtl_headers
add_files -norecurse $memory_files
set_property file_type {Verilog Header} [get_files [list \
    ov5640_color_delta_count.vh ov5640_isp_baseline_count.vh fpga_color_profile.vh]]
set_property file_type {Memory Initialization Files} \
    [get_files [list ov5640_isp_baseline.mem af_firmware.mem ov5640_vga_rgb565.mem ov5640_720p_rgb565.mem ov5640_color_delta.mem \
    fpga_color_gamma_r.mem fpga_color_gamma_g.mem fpga_color_gamma_b.mem \
    fpga_spatial_gain_r.mem fpga_spatial_gain_b.mem]]
set_property include_dirs [list [file join $root_dir hardware rtl]] \
    [get_filesets sources_1]

add_files -fileset constrs_1 -norecurse \
    [file join $root_dir hardware constraints ax7020_ddr_vdma_ov5640_hdmi.xdc]

set ::v300_bd_export_file [file join $project_dir system_bd_exported.tcl]
source [file join $root_dir hardware bd system_bd.tcl]
source [file join $root_dir scripts attach_fastest.tcl]
unset ::v300_bd_export_file
generate_target all [get_files system.bd]
make_wrapper -files [get_files system.bd] -top
set wrapper [file normalize [file join $project_dir ${project_name}.gen sources_1 bd system hdl system_wrapper.v]]
add_files -norecurse $wrapper

set_property top system_top [get_filesets sources_1]
update_compile_order -fileset sources_1
set_property STEPS.SYNTH_DESIGN.ARGS.FLATTEN_HIERARCHY rebuilt [get_runs synth_1]
set root_dir $workspace_root
set ready_file [open [file join $project_dir project_ready.txt] w]
puts $ready_file "MINI_PROJECT_CREATE_COMPLETE"
close $ready_file

puts "PROJECT_CREATED=$project_dir/$project_name.xpr"
puts "PROJECT_OPEN=$project_dir/$project_name.xpr"
puts "The project remains open for GUI use. In batch mode Vivado exits after this script finishes."
