# Read-only post-route evidence. Usage: -tclargs <report directory>
if {$argc != 1} {error "Pass routed report directory"}
set report_dir [file normalize [lindex $argv 0]]
open_checkpoint [file join $report_dir final_routed.dcp]
set camera_inputs [get_ports {cam_data[*] cam_href cam_vsync}]
set camera_registers [get_cells -hier -filter {NAME =~ *camera_axis_i/camera_data_reg* || NAME =~ *camera_axis_i/camera_href_reg* || NAME =~ *camera_axis_i/camera_vsync_reg*}]
if {[llength $camera_inputs] != 10 || [llength $camera_registers] != 10} {
 error "Expected all 10 DVP source-synchronous inputs and receiving registers"
}
report_timing -from $camera_inputs -to $camera_registers -delay_type max -max_paths 20 -nworst 2 -path_type full_clock_expanded -file [file join $report_dir camera_input_setup.rpt]
report_timing -from $camera_inputs -to $camera_registers -delay_type min -max_paths 20 -nworst 2 -path_type full_clock_expanded -file [file join $report_dir camera_input_hold.rpt]
set facts [open [file join $report_dir camera_input_facts.tsv] w]
foreach port $camera_inputs {
 foreach type {max min} {
  set paths [get_timing_paths -from $port -to $camera_registers -delay_type $type -max_paths 1]
  if {[llength $paths] != 1} {error "Unconstrained or missing $type path from $port"}
  puts $facts "$port\t$type\t[get_property SLACK $paths]"
 }
}
foreach c [get_clocks -include_generated_clocks cam_pclk] {
 puts $facts "clock:$c\t[get_property PERIOD $c]\t[get_property WAVEFORM $c]"
}
close $facts
close_design
