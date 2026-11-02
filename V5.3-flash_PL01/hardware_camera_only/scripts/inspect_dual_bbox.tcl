# Read-only fresh compiled/routed evidence. Run only after the candidate build.
if {$argc != 2} {error "Pass hardware_dual root and report directory"}
set root [file normalize [lindex $argv 0]]
set report [file normalize [lindex $argv 1]]
open_project -read_only [file join $root hardware_build vivado_camera_only camera_v32_384.xpr]
set compiled [open [file join $report compiled_sources.tsv] w]
foreach f [get_files -compile_order sources -used_in synthesis] {
 puts $compiled "SOURCE\t[file normalize [get_property NAME $f]]\t[get_property FILE_TYPE $f]"
}
close $compiled
open_checkpoint [file join $report final_routed.dcp]
set out [open [file join $report dual_bbox_compiled_paths.tsv] w]
set neural [get_cells -quiet -hier -filter {REF_NAME =~ *conv_accel* || NAME =~ *conv_accel*}]
puts $out "NO_CONV_ACCEL_CELLS=[expr {[llength $neural] == 0}]"
if {[llength $neural] != 0} {error "Unexpected neural cells: $neural"}
set overlay [get_cells -quiet bbox_i]
if {[llength $overlay] != 1} {error "Missing actual bbox overlay hierarchy"}
puts $out "bbox_overlay\t[get_property NAME $overlay]\t[get_property REF_NAME $overlay]"
foreach stem {bbox_shadow_gpio2 bbox_shadow_gpio3} {
 set cells [get_cells -quiet -hier -filter "NAME =~ *system_i/$stem"]
 if {[llength $cells] != 1} {error "Missing routed slot1 GPIO $stem"}
 puts $out "$stem\t[get_property NAME $cells]\t[get_property REF_NAME $cells]"
}
foreach stem {active0 active1 active2 active4 active5 active6} {
 set cells [get_cells -hier -filter "NAME =~ *bbox_i/${stem}_reg*"]
 if {[llength $cells] == 0} {error "Missing compiled slot state $stem"}
 set paths [get_timing_paths -from $cells -delay_type max -max_paths 1]
 if {[llength $paths] != 1} {error "No timed output path from slot state $stem"}
 foreach path $paths {
  puts $out "$stem\t[get_property STARTPOINT_PIN $path]\t[get_property ENDPOINT_PIN $path]\t[get_property SLACK $path]"
 }
}
foreach stem {meta4 meta5 meta6} {
 set pins [get_pins -hier -filter "NAME =~ *bbox_i/${stem}_reg*/D"]
 if {[llength $pins] == 0} {error "Missing added first-stage payload CDC $stem"}
 puts $out "$stem\t[llength $pins]\t$pins"
}
close $out
close_design
close_project
puts "DUAL_BBOX_COMPILED_PATH_INSPECTION_COMPLETE_BOARD_NOT_RUN"
