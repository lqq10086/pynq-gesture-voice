set hls_dir [file normalize [file dirname [info script]]]
set root [file dirname $hls_dir]
cd $hls_dir
open_project build_graph_csim
set_top fastest_accel
add_files [file join $hls_dir fastest_accel.cpp] -cflags "-std=c++11"
add_files -tb [file join $hls_dir test_hls_graph.cpp] -cflags "-std=c++11"
open_solution solution1 -flow_target vivado
set_part xc7z020clg400-2
config_interface -m_axi_addr64=false
create_clock -period 10 -name default
csim_design -clean -argv [file join $root reports physical_bundle_2402]
close_project
puts "PL01_FULL_GRAPH_CSIM_TCL_COMPLETE"
exit
