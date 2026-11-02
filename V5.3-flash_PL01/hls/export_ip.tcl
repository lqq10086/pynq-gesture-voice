# Package the already synthesized solution; no resynthesis or full-design build.
set hls_dir [file normalize [file dirname [info script]]]
cd $hls_dir
open_project build_csim
open_solution solution1 -flow_target vivado
export_design -format ip_catalog -rtl verilog -version 1.0
close_project
puts "PL01_QUALIFIED_HLS_IP_EXPORTED_NOT_ROUTED_NOT_BOARD"
exit
