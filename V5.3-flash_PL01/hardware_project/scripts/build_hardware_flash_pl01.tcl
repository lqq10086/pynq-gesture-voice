# Stage 1: synthesize/route/report only. No bit is published before host gates.
if {$argc != 2} {error "Pass new report directory and qualified HLS IP repository"}
set report_dir [file normalize [lindex $argv 0]]
set ::pl01_hls_repo [file normalize [lindex $argv 1]]
set root_dir [file normalize [file join [file dirname [info script]] ..]]
set project_dir [file join $root_dir hardware_build vivado_flash_pl01]
set project_file [file join $project_dir flash_pl01.xpr]
if {[version -short] ne "2023.1"} {error "Vivado 2023.1 required"}
if {[file exists $project_file]} {error "Existing PL01 project retained; automatic reset/reuse is forbidden"}

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

proc build_pl01 {} {
 global root_dir report_dir project_dir project_file argv argc
 source [file join $root_dir scripts create_hardware_flash_pl01.tcl]
 uplevel #0 [list open_bd_design [get_files system.bd]]
 set ps [require_one [get_bd_cells processing_system7_0] "Missing PS"]
 set core [require_one [get_bd_cells -hier -filter {VLNV =~ *:fastest_accel:1.0}] "Expected one Fastest BD core"]
 if {[string trimleft [get_property NAME $core] /] ne "fastest_accel_0"} {error "Unexpected Fastest instance"}
 if {[llength [get_bd_cells -quiet -hier -filter {VLNV =~ *:conv_accel:*}]] != 0} {error "Old conv_accel BD core present"}
 if {[get_property CONFIG.PCW_USE_S_AXI_HP1 $ps] != 1 || [get_property CONFIG.PCW_S_AXI_HP1_DATA_WIDTH $ps] != 64} {error "HP1 must be enabled and 64-bit"}
 if {[get_property CONFIG.NUM_MI [get_bd_cells axi_control_interconnect]] != 14} {error "Expected fourteen control masters"}
 set fclk0 [get_bd_nets -of_objects [get_bd_pins $ps/FCLK_CLK0]]
 foreach pin [list $core/ap_clk $ps/S_AXI_HP1_ACLK axi_flash_memory/ACLK axi_flash_memory/M00_ACLK] {
  if {[get_bd_nets -of_objects [get_bd_pins $pin]] ne $fclk0} {error "New HP1/core clock differs from FCLK0: $pin"}
 }
 validate_bd_design
 save_bd_design
 set address_file [open [file join $report_dir address_map.tsv] w]
 foreach segment [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data]] {puts $address_file "$segment\t[get_property OFFSET $segment]\t[get_property RANGE $segment]"}
 close $address_file
 foreach {pattern offset} {*fastest_accel_0* 0x43C00000 *flash_phase_gpio* 0x412D0000 *camera_control_0* 0x41220000 *bbox_shadow_gpio2* 0x412B0000 *bbox_shadow_gpio3* 0x412C0000} {
  set segment [require_one [get_bd_addr_segs -of_objects [get_bd_addr_spaces $ps/Data] -filter "NAME =~ $pattern"] "Missing or repeated AXI register segment: $pattern"]
  if {[expr [get_property OFFSET $segment]] != [expr $offset] || [expr [get_property RANGE $segment]] != 65536} {error "Wrong AXI range for $pattern"}
 }
 set bd_facts [open [file join $report_dir bd_facts.tsv] w]
 puts $bd_facts "fastest_core_count\t1\nold_conv_core_count\t0\nhp1_enabled\t1\nhp1_width\t64\ncontrol_MI\t14\nnew_hp1_clock\tFCLK0"
 close $bd_facts
 generate_target all [get_files system.bd]
 update_compile_order -fileset sources_1
 set bd [get_files system.bd]
 set original_mode [get_property synth_checkpoint_mode $bd]
 set_property synth_checkpoint_mode None $bd
 update_compile_order -fileset sources_1
 set failed [catch {synth_design -rtl -top system_top -part xc7z020clg400-2} message options]
 set_property synth_checkpoint_mode $original_mode $bd
 if {$failed} {return -options $options $message}
 close_design
 generate_target all $bd
 update_compile_order -fileset sources_1
 report_compile_order -used_in synthesis -file [file join $report_dir compiled_inventory.rpt]
 puts "PL01_FULL_RTL_ELABORATION_PASS"
 launch_runs synth_1 -jobs 2
 wait_on_run synth_1
 if {[get_property STATUS [get_runs synth_1]] ne "synth_design Complete!"} {error "PL01 synthesis failed"}
 open_run synth_1
 verify_core synthesis
 report_utilization -hierarchical -file [file join $report_dir synthesis_utilization.rpt]
 close_design
 set_property strategy Performance_ExplorePostRoutePhysOpt [get_runs impl_1]
 launch_runs impl_1 -to_step route_design -jobs 2
 wait_on_run impl_1
 # A post-route strategy can report "Not started phys_opt_design (Post-Route)"
 # after a successful -to_step route_design. Use its checkpoint and physical facts.
 set routed_checkpoint [file join $project_dir flash_pl01.runs impl_1 system_top_routed.dcp]
 if {[get_property NEEDS_REFRESH [get_runs impl_1]] || ![file exists $routed_checkpoint]} {error "PL01 routed checkpoint is missing or stale"}
 open_run impl_1
 if {![report_route_status -boolean_check ROUTED_FULLY] || [report_route_status -boolean_check ERRORS_IN_ROUTES] || [report_route_status -boolean_check PARTIALLY_ROUTED]} {error "PL01 physical route is incomplete or erroneous"}
 phys_opt_design -directive AggressiveExplore
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
 report_ip_status -file [file join $report_dir ip_status.rpt]
 set facts [open [file join $report_dir facts.tsv] w]
 foreach type {ROUTED_FULLY ERRORS_IN_ROUTES PARTIALLY_ROUTED} {puts $facts "$type\t[report_route_status -boolean_check $type]"}
 puts $facts "setup_bad\t[llength [get_timing_paths -delay_type max -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "hold_bad\t[llength [get_timing_paths -delay_type min -slack_lesser_than 0 -max_paths 1]]"
 puts $facts "drc_blocking\t[llength [get_drc_violations -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]]"
 puts $facts "locked_ips\t[llength [get_ips -filter {IS_LOCKED == 1}]]"
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
}

if {[catch {build_pl01} message options]} {
 puts stderr "PL01_BUILD_FAIL: $message"
 return -options $options $message
}
