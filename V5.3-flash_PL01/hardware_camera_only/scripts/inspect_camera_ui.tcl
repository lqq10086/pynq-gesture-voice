# Read-only, actual routed UI/camera hierarchy and timed paths.
if {$argc != 1} {error "Pass camera_only_build report directory"}
set report [file normalize [lindex $argv 0]]
open_checkpoint [file join $report final_routed.dcp]
set out [open [file join $report camera_ui_compiled_paths.tsv] w]
foreach stem {ui_blend_i ui_present_i ui_status_i} {
 set cell [get_cells -quiet $stem]
 if {[llength $cell] != 1} {error "Missing actual UI hierarchy $stem"}
 puts $out "$stem\t[get_property NAME $cell]\t[get_property REF_NAME $cell]"
 set regs [filter [all_registers -cells] "NAME =~ ${stem}/*"]
 if {[llength $regs] == 0} {error "Missing actual UI sequential state $stem"}
 set paths [get_timing_paths -from $regs -delay_type max -max_paths 1]
 if {[llength $paths] != 1} {error "Missing timed UI state path $stem"}
 foreach path $paths {
  puts $out "${stem}_timed\t[get_property STARTPOINT_PIN $path]\t[get_property ENDPOINT_PIN $path]\t[get_property SLACK $path]"
 }
}
foreach stem {ui_vdma axis_ui_cdc axis_ui_fifo axi_ui_memory camera_control_0 axi_vdma_0} {
 set cells [get_cells -quiet -hier -filter "NAME =~ *system_i/$stem"]
 if {[llength $cells] != 1} {error "Missing routed camera/UI peripheral $stem"}
 puts $out "$stem\t[get_property NAME $cells]\t[get_property REF_NAME $cells]"
}
close $out
close_design
puts "ACTUAL_CAMERA_UI_HIERARCHY_AND_TIMED_PATHS_VERIFIED_BOARD_NOT_RUN"
