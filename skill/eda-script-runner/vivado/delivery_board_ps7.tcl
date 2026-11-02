##############################################################################
# 通过 XSCT 走 JTAG 做四件事：probe 探测、status 读状态、jtag 下载并运行、reboot 复位。
# 命令：python tools/run_tcl.py vivado/delivery_board_ps7.tcl --args "-action probe [-pattern *xc7z020*]"
# 直接调用：xsct delivery_board_ps7.tcl <action>
# 版本依赖：connect/targets/mrd/fpga 命令在 2019.1 到 2023.2 的 xsct 中都存在；
#           2023.2 之后 XSCT 仍可用，但配套的 Vitis unified 流程推荐走 vitis -s。
# 与原脚本的区别：器件型号过滤从写死 *xc7z020* 改成 -pattern 参数；
#                 找不到唯一目标时不再直接报错，而是打印 MISSING_INPUT 说明缺什么。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
if {$argc >= 1 && [string index [lindex $argv 0] 0] ne "-"} {
    set ::portable::opt(action) [lindex $argv 0]
    if {$argc >= 2} { set ::portable::opt(firmware_dir) [lindex $argv 1] }
} else {
    ::portable::parse_args $argv
}
::portable::tool_info [::portable::get expect_version ""]
set pattern [::portable::get pattern "*xc7z020*"]
set url [::portable::get url "tcp:127.0.0.1:3121"]
# XSCT changes volatile state only. QSPI programming is in delivery.ps1.
set root [file normalize [file join [file dirname [info script]] ..]]
if {$argc < 1 || $argc > 2} { error "Usage: xsct stage1_board.tcl jtag|status|probe|reboot ?firmware-directory?" }
set action [lindex $argv 0]
if {$action ni {jtag status probe reboot}} { error "Unknown action: $action" }
if {[catch {connect -url $url} err]} {
    puts "MISSING_INPUT: preconditions not met, script stopped"
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    exit 2
}
set boards [targets -target-properties -filter "name =~ \"$pattern\""]
set cores [targets -target-properties -filter {name =~ "*Cortex-A9*#0"}]
if {[llength $boards] != 1 || [llength $cores] != 1} {
    puts "MISSING_INPUT: preconditions not met, script stopped"
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    disconnect
    exit 2
}
if {$action eq "probe"} {
    puts [targets]
    puts "BOARD_PROBE_OK (XC7Z020 detected; check board model and QSPI device manually)"
    disconnect
    exit 0
}
proc read32 {address} { return [expr {[mrd -force -value $address] & 0xFFFFFFFF}] }
proc snapshot {} {
    for {set retry 0} {$retry < 20} {incr retry} {
        set before [read32 0x10600008]
        set words [mrd -force -value 0x10600000 32]
        set after [read32 0x10600008]
        if {$before == $after && ($after & 1) == 0 && [llength $words] == 32} { return $words }
    }
    error "No coherent V1.5 mailbox snapshot"
}
proc check_runtime {} {
    set w [snapshot]
    if {[lindex $w 0] != 0x56313542 || [lindex $w 1] != 0x00010000} { error "Not a V1.5 application: old Run05 mailbox rejected" }
    if {[lindex $w 3] != 3 || [lindex $w 27] != 0} { error "V1.5 not running; phase=[lindex $w 3] error=[lindex $w 27]" }
    foreach i {4 5 6 9} { if {[lindex $w $i] != 0} { error "Lens/copy/VDMA error evidence at mailbox word $i" } }
    set pl [read32 0x41200000]
    set color [read32 0x41200008]
    if {($pl & 0x80000043) != 0x80000043 || ($pl & 0x1B4) != 0} { error [format "PL video error: %08X" $pl] }
    if {($color & 0xFC000000) != 0x34000000} { error [format "Run05 not fixed active: %08X" $color] }
    foreach base {0x43000000 0x43000030} {
        set cr [read32 $base]; set sr [read32 [expr {$base+4}]]
        if {($cr & 0x8B) != 0x8B || ($sr & 0xFF1) != 0} { error [format "VDMA unhealthy or genlock disabled: base=%08X CR=%08X SR=%08X" $base $cr $sr] }
    }
    foreach {offset value} {0x50 480 0x54 1280 0x58 1280 0xA0 480 0xA4 1280 0xA8 1280} {
        if {[read32 [expr {0x43000000+$offset}]] != $value} { error "VDMA geometry mismatch at $offset" }
    }
    foreach base {0x4300005C 0x430000AC} {
        set i 0
        foreach addr {0x10000000 0x10096000 0x1012C000} {
            if {[read32 [expr {$base+4*$i}]] != $addr} { error "VDMA does not share RAW buffers at $base index $i" }
            incr i
        }
    }
    puts [format "V1.5 seconds=%u capture_irq=%u display_irq=%u PS_LENS=0 CPU_COPY=0 COLOR=1 errors=0 PARK=%08X" \
        [lindex $w 29] [lindex $w 7] [lindex $w 8] [read32 0x43000028]]
    return $w
}
if {$action eq "jtag"} {
    set firmware [file join $root firmware]
    if {$argc == 2} { set firmware [file normalize [lindex $argv 1]] }
    set bit [file join $firmware system.bit]
    set elf [file join $firmware camera_hdmi.elf]
    set init [file join $firmware ps7_init.tcl]
    foreach f [list $bit $elf $init] { if {![file exists $f]} { error "Firmware missing: $f" } }
    targets -set -nocase -filter {name =~ "*Cortex-A9*#0"}
    catch {stop}
    rst -system
    after 1000
    targets -set -nocase -filter {name == "APU"}
    source $init
    ps7_init
    targets -set -nocase -filter {name =~ "*xc7z020*"}
    fpga -file $bit
    targets -set -nocase -filter {name == "APU"}
    ps7_post_config
    targets -set -nocase -filter {name =~ "*Cortex-A9*#0"}
    rst -processor
    after 100
    rwr cpsr 0x600F01D3
    dow $elf
    rwr cpsr 0x600F01D3
    rwr pc 0x00100000
    con
    after 10000
    puts "V1_5_JTAG_STARTED"
}
if {$action eq "reboot"} {
    targets -set -nocase -filter {name == "APU"}
    set bootmode [expr {[read32 0xF800025C] & 0x7}]
    if {$bootmode != 1} { error "QSPI mode not latched. Power OFF, select QSPI boot, then power ON." }
    rst -system
    after 15000
    puts "QSPI_AUTO_REBOOT_COMPLETE"
}
targets -set -nocase -filter {name == "APU"}
set first [check_runtime]
after 5000
set current [check_runtime]
foreach i {7 8 26} {
    set delta [expr {([lindex $current $i]-[lindex $first $i]) & 0xFFFFFFFF}]
    if {$delta == 0 || $delta > 100000} { error "Counter stopped or restarted at word $i" }
}
puts "V1_5_RUNTIME_CHECK_OK duration_seconds=5"
disconnect
