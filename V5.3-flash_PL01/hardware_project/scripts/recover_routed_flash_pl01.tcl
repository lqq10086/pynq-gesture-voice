# Reports-only recovery: opens a retained exact checkpoint; never launches a run.
if {$argc != 3} {error {Expected new report directory, exact route checkpoint and existing project}}
set report_dir [file normalize [lindex $argv 0]]
set origin_checkpoint [file normalize [lindex $argv 1]]
set origin_project [file normalize [lindex $argv 2]]
set root_dir [file normalize [file join [file dirname [info script]] ..]]
set project_dir [file join $root_dir hardware_build vivado_flash_pl01]
if {[version -short] ne {2023.1}} {error {Vivado 2023.1 required}}
if {$origin_project ne [file normalize [file join $project_dir flash_pl01.xpr]] || $origin_checkpoint ne [file normalize [file join $project_dir flash_pl01.runs impl_1 system_top_routed.dcp]]} {error {Exact retained main project and checkpoint required}}
if {![file exists $origin_checkpoint]} {error {Missing retained routed checkpoint}}
open_project $origin_project
if {[get_property NEEDS_REFRESH [get_runs impl_1]]} {error {Retained implementation needs refresh}}
set project_ips [get_ips -all]
if {[llength $project_ips] == 0} {error {Retained project has no IP instances; cannot qualify IP status}}
report_ip_status -file [file join $report_dir ip_status.rpt]
if {![file exists [file join $report_dir ip_status.rpt]] || [file size [file join $report_dir ip_status.rpt]] == 0} {error {Actual project IP report missing}}
set project_locked_ips [llength [get_ips -all -filter {IS_LOCKED == 1}]]
if {$project_locked_ips != 0} {error {Retained project contains locked IP}}
open_checkpoint $origin_checkpoint
if {![report_route_status -boolean_check ROUTED_FULLY] || [report_route_status -boolean_check ERRORS_IN_ROUTES] || [report_route_status -boolean_check PARTIALLY_ROUTED]} {error {Retained checkpoint is not fully routed}}
proc require_one {objects message} {if {[llength $objects] != 1} {error $message};return [lindex $objects 0]}
proc verify_core {stage} {
 global report_dir
 set core [get_cells -quiet -hier -filter {NAME =~ *system_i/fastest_accel_0}]
 if {[llength $core] != 1} {error "Expected exactly one actual Fastest core at $stage"}
 set old [get_cells -quiet -hier -filter {NAME =~ *conv_accel* || REF_NAME =~ *conv_accel*}]
 if {[llength $old] != 0} {error "Old YOLOv5 neural core is present"}
 set out [open [file join $report_dir ${stage}_core.tsv] w]
 puts $out "fastest_core_count\t[llength $core]"
 puts $out "old_conv_core_count\t[llength $old]"
 puts $out "core_path\t$core"
 close $out
}

 verify_core routed
 set dual [open [file join $report_dir dual_bbox_routed.tsv] w]
 foreach stem {active0 active1 active2 active4 active5 active6 pending4 pending5 pending6 meta4 meta5 meta6} {
  set cells [get_cells -hier -filter "NAME =~ *bbox_i/${stem}_reg*"]
  if {[llength $cells] == 0} {error "Missing real routed dual-slot state: $stem"}
  puts $dual "$stem\t[llength $cells]\t$cells"
 }
 foreach stem {bbox_shadow_gpio2 bbox_shadow_gpio3} {
  set cells [get_cells -hier -filter "NAME =~ *system_i/$stem"]
  if {[llength $cells] != 1} {error "Missing routed dual-slot GPIO: $stem"}
  puts $dual "$stem\t[get_property REF_NAME $cells]"
 }
 close $dual
 report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose -max_paths 20 -file [file join $report_dir timing_summary.rpt]
 report_pulse_width -all_violators -warn_on_violation -file [file join $report_dir pulse_width.rpt]
 report_bus_skew -delay_type min_max -warn_on_violation -file [file join $report_dir bus_skew.rpt]
 report_utilization -hierarchical -file [file join $report_dir utilization.rpt]
 report_drc -file [file join $report_dir drc.rpt]
 report_cdc -details -all_checks_per_endpoint -file [file join $report_dir cdc.rpt]
 report_cdc -details -all_checks_per_endpoint -no_waiver -file [file join $report_dir cdc_unwaived_view.rpt]
 report_clocks -file [file join $report_dir clocks.rpt]
 report_clock_interaction -delay_type min_max -file [file join $report_dir clock_interaction.rpt]
 report_exceptions -coverage -file [file join $report_dir exceptions_coverage.rpt]
 report_exceptions -ignored -file [file join $report_dir exceptions_ignored.rpt]
 check_timing -verbose -file [file join $report_dir check_timing.rpt]
 report_route_status -file [file join $report_dir route_status.rpt]
 set facts [open [file join $report_dir facts.tsv] w]
 foreach type {ROUTED_FULLY ERRORS_IN_ROUTES PARTIALLY_ROUTED} {puts $facts "$type\t[report_route_status -boolean_check $type]"}
 puts $facts "setup_bad\t[llength [get_timing_paths -delay_type max -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "hold_bad\t[llength [get_timing_paths -delay_type min -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "drc_blocking\t[llength [get_drc_violations -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]]"
 puts $facts "locked_ips\t$project_locked_ips"
 foreach clk [get_clocks] {puts $facts "clock:$clk\t[get_property PERIOD $clk]"}
 close $facts
 foreach name {cam_pclk cam_vsync cam_href cam_reset_n cam_pwdn cam_scl cam_sda hdmi_clk_p hdmi_clk_n hdmi_out_en color_sw0 color_sw1} {
  if {[llength [get_ports -quiet $name]] != 1} {error "Missing preserved physical port $name"}
 }
 write_checkpoint [file join $report_dir final_routed.dcp]
 close_design
 set saved_argv $argv
 set saved_argc $argc
 set argv [list $report_dir]
 set argc 1
 source [file join $root_dir scripts report_v33_camera_paths.tcl]
 set argv $saved_argv
 set argc $saved_argc
 set hwh [file join $project_dir flash_pl01.gen sources_1 bd system hw_handoff system.hwh]
 file copy $hwh [file join $report_dir flash_pl01.hwh]
 set stage [open [file join $report_dir route_stage_complete.txt] w]
 puts $stage "ROUTED_CANDIDATE_ONLY_HOST_GATES_REQUIRED_NO_BIT_PUBLISHED"
 close $stage
close_project
