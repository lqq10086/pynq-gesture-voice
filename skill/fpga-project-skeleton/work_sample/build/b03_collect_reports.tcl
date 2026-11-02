# Purpose: turn one build's reports into a machine-readable summary for gate checks and report citations.
# Input  : a run directory holding timing_summary.rpt and utilization.rpt; cdc.rpt and drc.rpt optional.
# Output : <run>/gate_summary.json, fixed fields, missing values are null (never "NA").
# Exit   : 0 collected; 2 a report exists but key fields cannot be parsed; 3 report or argument missing.
# Tools  : Vivado 2020.1+; verified on 2023.2. A localized (non-English) report will fail to parse and return 2.
# Note   : messages are ASCII on purpose. Vivado reads .tcl with the system code page on Chinese Windows,
#          so non-ASCII text printed from a script shows up as garbage.

proc fail {code msg} { puts "ERROR: $msg"; exit $code }

# args: run directory is required; -out overrides the summary path
set run_dir ""
set out_path ""
for {set i 0} {$i < [llength $argv]} {incr i} {
    set key [lindex $argv $i]
    switch -- $key {
        -run_dir { set run_dir  [lindex $argv [incr i]] }
        -out     { set out_path [lindex $argv [incr i]] }
        default  { if {$run_dir eq ""} { set run_dir $key } }
    }
}
if {$run_dir eq ""} { fail 3 "usage: b03_collect_reports.tcl -run_dir <run dir> \[-out <summary path>\]" }
if {![file isdirectory $run_dir]} { fail 3 "not a directory: $run_dir" }
if {$out_path eq ""} { set out_path [file join $run_dir gate_summary.json] }

foreach name {timing_summary.rpt utilization.rpt} {
    if {![file exists [file join $run_dir $name]]} { fail 3 "missing $name" }
}

proc slurp {path} {
    set fp [open $path r]; set d [read $fp]; close $fp; return $d
}

proc columns {line} {
    # Reports pad columns with spaces, not delimiters. Splitting on two or more spaces
    # keeps multi-word labels ("TNS Failing Endpoints") as one column.
    set line [string trim $line]
    if {$line eq ""} { return {} }
    set out {}
    foreach c [regexp -all -inline {[^ ]+(?: [^ ]+)*} $line] {
        if {[string first "|" $c] >= 0} { continue }
        lappend out [string trim $c]
    }
    return $out
}

proc pipe_cells {line} {
    if {[string first "|" $line] < 0} { return {} }
    set out {}
    foreach c [split [string trim $line] "|"] {
        set c [string trim $c]
        if {$c ne ""} { lappend out $c }
    }
    return $out
}

proc is_num {v} { return [regexp {^-?[0-9]+\.?[0-9]*$} $v] }

proc summary_value {text key} {
    # The timing table puts several metrics on one header line (WNS(ns), TNS(ns), ...)
    # and the values on the next line, so the column index matters. Taking "the first
    # number after the header" reports TNS as if it were WNS.
    set lines [split $text "\n"]
    for {set i 0} {$i < [llength $lines]} {incr i} {
        set header [columns [lindex $lines $i]]
        set col -1
        for {set c 0} {$c < [llength $header]} {incr c} {
            if {[string first "${key}(ns)" [lindex $header $c]] == 0} { set col $c; break }
        }
        if {$col < 0} { continue }
        for {set j [expr {$i + 1}]} {$j < [llength $lines]} {incr j} {
            set cells [columns [lindex $lines $j]]
            if {[llength $cells] <= $col} { continue }
            set v [lindex $cells $col]
            if {[is_num $v]} { return $v }
        }
    }
    return ""
}

proc utilization_row {text labels} {
    # Match the first label that exists; return {used available pct}.
    foreach line [split $text "\n"] {
        set cells [pipe_cells $line]
        if {[llength $cells] < 4} { continue }
        set name [lindex $cells 0]
        foreach label $labels {
            if {$name ne $label} { continue }
            set pct ""
            for {set c [expr {[llength $cells] - 1}]} {$c >= 1} {incr c -1} {
                set v [string trim [lindex $cells $c] "%"]
                if {[is_num $v]} { set pct $v; break }
            }
            set used [lindex $cells 1]
            set avail ""
            if {[llength $cells] >= 6} { set avail [lindex $cells [expr {[llength $cells] - 2}]] }
            return [list $used $avail $pct]
        }
    }
    return {}
}

proc util_pct {text labels} {
    set row [utilization_row $text $labels]
    if {[llength $row] == 0} { return "" }
    return [lindex $row 2]
}

set timing [slurp [file join $run_dir timing_summary.rpt]]
set util   [slurp [file join $run_dir utilization.rpt]]

set wns [summary_value $timing "WNS"]
set whs [summary_value $timing "WHS"]
set tns [summary_value $timing "TNS"]
if {$wns eq ""} { fail 2 "cannot parse WNS from timing_summary.rpt; a localized report needs an English run" }

set lut  [util_pct $util {CLB LUTs {Slice LUTs}}]
set ff   [util_pct $util {CLB Registers {Register as Flip Flop}}]
set bram [util_pct $util {Block RAM Tile}]
set dsp  [util_pct $util {DSPs {DSP Blocks}}]
if {$lut eq ""} { puts "WARNING: no LUT row in utilization.rpt; percentages stay null" }

# CDC / DRC: only count lines and keep the most severe level; the verdict belongs to the gate report.
proc count_lines {path pattern} {
    if {![file exists $path]} { return "" }
    set n 0
    foreach line [split [slurp $path] "\n"] { if {[regexp $pattern $line]} { incr n } }
    return $n
}
set cdc_critical [count_lines [file join $run_dir cdc.rpt] {^CRITICAL}]
set cdc_warning  [count_lines [file join $run_dir cdc.rpt] {^WARNING}]
set drc_errors   [count_lines [file join $run_dir drc.rpt] {^ERROR|^Violations found}]

proc json_num {v} {
    if {$v eq ""} { return "null" }
    if {[string is double -strict $v]} { return $v }
    return "null"
}
proc json_str {v} {
    if {$v eq ""} { return "null" }
    return "\"$v\""
}

set fp [open $out_path w]
puts $fp "{"
puts $fp "  \"tool_version\": \"[version -short]\","
set part_name ""
if {[llength [info commands current_project]] > 0 && [current_project -quiet] ne ""} {
    set part_name [get_property PART [current_project]]
}
if {$part_name eq ""} {
    # running standalone, no project open: take the device from the report header
    foreach line [split $util "\n"] {
        if {[regexp {\|\s*Device\s*:\s*(\S+)} $line -> d]} { set part_name $d; break }
    }
}
puts $fp "  \"parts\": [json_str $part_name],"
puts $fp "  \"wns_ns\": [json_num $wns],"
puts $fp "  \"whs_ns\": [json_num $whs],"
puts $fp "  \"tns_ns\": [json_num $tns],"
puts $fp "  \"lut_pct\": [json_num $lut],"
puts $fp "  \"ff_pct\": [json_num $ff],"
puts $fp "  \"bram_pct\": [json_num $bram],"
puts $fp "  \"dsp_pct\": [json_num $dsp],"
puts $fp "  \"cdc_critical_lines\": [json_num $cdc_critical],"
puts $fp "  \"cdc_warning_lines\": [json_num $cdc_warning],"
puts $fp "  \"drc_error_lines\": [json_num $drc_errors]"
puts $fp "}"
close $fp

puts "RESULT: summary written to $out_path"
puts "RESULT: WNS=$wns WHS=$whs TNS=$tns LUT%=$lut FF%=$ff BRAM%=$bram DSP%=$dsp"
exit 0
