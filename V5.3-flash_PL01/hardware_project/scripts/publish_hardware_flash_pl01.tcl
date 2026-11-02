# Stage 2: called only after host routed/CDC/camera gates pass.
if {$argc != 2} {error "Pass verified report directory and host Python executable"}
set report_dir [file normalize [lindex $argv 0]]
set python [lindex $argv 1]
set verifier [file normalize [file join [file dirname [info script]] .. .. build_hardware.py]]
if {[version -short] ne "2023.1"} {error "Vivado 2023.1 required"}
if {![file exists [file join $report_dir route_admission.json]]} {error "No host routed admission"}
if {[catch {exec $python -B $verifier --verify-route $report_dir} verification]} {error "Host routed/source admission failed: $verification"}
if {[file exists [file join $report_dir flash_pl01.bit]]} {error "Never overwrite a published candidate bit"}
open_checkpoint [file join $report_dir final_routed.dcp]
report_drc -file [file join $report_dir publish_drc.rpt]
if {[llength [get_timing_paths -delay_type max -slack_lesser_than 0 -max_paths 1]] || [llength [get_timing_paths -delay_type min -slack_lesser_than 0 -max_paths 1]]} {error "Routed timing no longer passes"}
if {[llength [get_drc_violations -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]]} {error "Blocking routed DRC remains"}
if {![report_route_status -boolean_check ROUTED_FULLY] || [report_route_status -boolean_check ERRORS_IN_ROUTES] || [report_route_status -boolean_check PARTIALLY_ROUTED]} {error "Route status failed before bit generation"}
write_bitstream [file join $report_dir flash_pl01.bit]
close_design
puts "PL01_BIT_CREATED_AFTER_HOST_ROUTED_GATES_NOT_BOARD_QUALIFIED"
