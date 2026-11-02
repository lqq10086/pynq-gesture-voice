set hls_dir [file normalize [file dirname [info script]]]
cd $hls_dir
open_project build_csim
set_top fastest_accel
add_files [file join $hls_dir fastest_accel.cpp] -cflags "-std=c++11"
open_solution solution1 -flow_target vivado
set_part xc7z020clg400-2
config_interface -m_axi_addr64=false
create_clock -period 10 -name default
csynth_design
close_project
puts "PL01_HLS_SYNTHESIS_COMPLETE_NOT_ROUTED_NOT_BOARD"
exit
