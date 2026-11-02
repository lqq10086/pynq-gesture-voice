# Full video + shared 384 accelerator; candidate publication needs routed review.
set root_dir [file normalize [file join [file dirname [info script]] ..]]
if {$argc < 1 || $argc > 2} {error "Start with scripts/build_hardware_384_D.py"}
set report_dir [file normalize [lindex $argv 0]]
set resume_synthesis [expr {$argc == 2 && [lindex $argv 1] eq "resume_synthesis"}]
proc camera_only_evidence {stage} {
 global report_dir
 set neural [get_cells -quiet -hier -filter {REF_NAME =~ *conv_accel* || NAME =~ *conv_accel* || NAME =~ *accel_timer* || NAME =~ *axi_ai_memory*}]
 if {[llength $neural] != 0} {error "Neural cells unexpectedly present: $neural"}
 set out [open [file join $report_dir ${stage}_camera_only.tsv] w]
 puts $out "neural_cells\t0"
 set dsps [get_cells -quiet -hier -filter {REF_NAME =~ DSP48*}]
 puts $out "video_dsp_count\t[llength $dsps]"
 foreach cell $dsps {puts $out "video_dsp\t[get_property NAME $cell]\t[get_property REF_NAME $cell]"}
 close $out
}

proc mini_build {} {
 global root_dir report_dir argv argc resume_synthesis
 if {[version -short] ne "2023.1"} {error "Vivado 2023.1 required"}
 set project_dir [file join $root_dir hardware_build vivado_camera_only]
 set source_root $root_dir
 set project_file [file join $project_dir camera_v32_384.xpr]
 if {[file exists $project_file]} {
  open_project $project_file
  add_files -norecurse [glob [file join $source_root hardware rtl *.v]]
  if {!$resume_synthesis} {reset_run synth_1}
 } else {
  if {$resume_synthesis} {error "Cannot resume without an existing project"}
  source [file join $root_dir scripts create_hardware_camera_only.tcl]
 }
 # Vivado 2023.1's AXI interconnect BD callbacks source relative helper files;
 # invoke at global Tcl scope, as the GUI and standalone BD script do.
 uplevel #0 [list open_bd_design [get_files system.bd]]
 set neural [get_bd_cells -quiet -hier -filter {VLNV =~ *:conv_accel:* || NAME =~ *conv_accel* || NAME =~ *accel_timer* || NAME =~ *axi_ai_memory*}]
 if {[llength $neural] != 0} {error "Neural BD branch still present"}
 if {[get_property CONFIG.PCW_USE_S_AXI_HP1 [get_bd_cells processing_system7_0]] != 0} {error "Neural HP1 port still enabled"}
 validate_bd_design
 save_bd_design
 if {!$resume_synthesis} {
 generate_target all [get_files system.bd]
 update_compile_order -fileset sources_1
 # Expand the actual top and all generated IP RTL before spending time on OOC
 # synthesis. A newly regenerated OOC IP has no DCP/stub for -rtl_skip_ip yet.
 set bd [get_files system.bd]
 set original_mode [get_property synth_checkpoint_mode $bd]
 set_property synth_checkpoint_mode None $bd
 update_compile_order -fileset sources_1
 set elab_failed [catch {synth_design -rtl -top system_top -part xc7z020clg400-2} elab_message elab_options]
 set_property synth_checkpoint_mode $original_mode $bd
 if {$elab_failed} {return -options $elab_options $elab_message}
 close_design
 generate_target all $bd
 update_compile_order -fileset sources_1
 puts "V33_TOP_RTL_ELABORATION_PASS"
 }
 set address_file [open [file join $report_dir address_map.txt] w]
 foreach segment [get_bd_addr_segs -of_objects [get_bd_addr_spaces processing_system7_0/Data]] {
  puts $address_file "$segment [get_property OFFSET $segment] [get_property RANGE $segment]"
 }
 close $address_file
 foreach {pattern offset} {*camera_control_0* 0x41220000 *bbox_shadow_gpio2* 0x412B0000 *bbox_shadow_gpio3* 0x412C0000} {
  set segments [get_bd_addr_segs -filter "NAME =~ $pattern" -of_objects [get_bd_addr_spaces processing_system7_0/Data]]
  if {[llength $segments] != 1 || [get_property OFFSET $segments] != $offset} {error "Unexpected address map: $pattern"}
 }
 puts "MINI_BD_VALIDATE_PASS"
 if {!$resume_synthesis} {
  launch_runs synth_1 -jobs 2
  wait_on_run synth_1
 } else {
  puts "V33_RESUMING_VERIFIED_SYNTHESIS_CHECKPOINT"
 }
 if {[get_property STATUS [get_runs synth_1]] ne "synth_design Complete!"} {error "Synthesis failed"}
 open_run synth_1
 camera_only_evidence synthesis
 report_utilization -hierarchical -file [file join $report_dir synthesis_utilization.rpt]
 close_design
 puts "MINI_SYNTHESIS_PASS"
 reset_run impl_1
 set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]
 launch_runs impl_1 -to_step write_bitstream -jobs 2
 wait_on_run impl_1
 if {[get_property STATUS [get_runs impl_1]] ne "write_bitstream Complete!"} {error "Implementation/bitstream failed"}
 open_run impl_1
 camera_only_evidence routed
 set dual_facts [open [file join $report_dir dual_bbox_routed.tsv] w]
 foreach stem {active0 active1 active2 active4 active5 active6 pending4 pending5 pending6 meta4 meta5 meta6} {
  set cells [get_cells -hier -filter "NAME =~ *bbox_i/${stem}_reg*"]
  if {[llength $cells] == 0} {error "Missing routed dual-slot state: $stem"}
  puts $dual_facts "$stem\t[llength $cells]\t$cells"
 }
 foreach stem {bbox_shadow_gpio2 bbox_shadow_gpio3} {
  set cells [get_cells -hier -filter "NAME =~ *system_i/$stem"]
  if {[llength $cells] != 1} {error "Missing routed second-slot GPIO: $stem"}
  puts $dual_facts "$stem\t[get_property REF_NAME $cells]"
 }
 close $dual_facts
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
 report_ip_status -file [file join $report_dir ip_status.rpt]
 set facts [open [file join $report_dir facts.tsv] w]
 foreach type {ROUTED_FULLY ERRORS_IN_ROUTES PARTIALLY_ROUTED} {
  puts $facts "$type\t[report_route_status -boolean_check $type]"
 }
 puts $facts "setup_bad\t[llength [get_timing_paths -delay_type max -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "hold_bad\t[llength [get_timing_paths -delay_type min -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "drc_blocking\t[llength [get_drc_violations -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]]"
 puts $facts "locked_ips\t[llength [get_ips -filter {IS_LOCKED == 1}]]"
 foreach clk [get_clocks] {puts $facts "clock:$clk\t[get_property PERIOD $clk]"}
 puts $facts "neural_cells\t0"
 close $facts
 foreach name {cam_pclk cam_vsync cam_href cam_reset_n cam_pwdn cam_scl cam_sda hdmi_clk_p hdmi_clk_n hdmi_out_en color_sw0 color_sw1} {
  if {[llength [get_ports -quiet $name]] != 1} {error "Missing physical port $name"}
 }
 write_checkpoint -force [file join $report_dir final_routed.dcp]
 close_design
 set saved_argv $argv
 set saved_argc $argc
 set argv [list $report_dir]
 set argc 1
 source [file join $root_dir scripts report_v33_camera_paths.tcl]
 set argv $saved_argv
 set argc $saved_argc
 set bit [file join $project_dir camera_v32_384.runs impl_1 system_top.bit]
 set hwh [file join $project_dir camera_v32_384.gen sources_1 bd system hw_handoff system.hwh]
 file copy -force $bit [file join $report_dir camera_v32_384.bit]
 file copy -force $hwh [file join $report_dir camera_v32_384.hwh]
 set result [open [file join $report_dir build_result.txt] w]
 puts $result "CAMERA_ONLY_DUAL_BBOX_CANDIDATE_COMPLETE_REQUIRES_ROUTED_REVIEW"
 puts $result "BOARD_VALIDATION_PENDING"
 close $result
 close_project
}

if {[catch {mini_build} message options]} {
 puts stderr "V32_BUILD_FAIL: $message"
 return -options $options $message
}
