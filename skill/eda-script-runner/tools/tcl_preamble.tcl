##############################################################################
# 通用前置：参数解析、版本告警、缺输入上报。
# 被同目录树下的 Tcl 脚本 source，不单独执行。
#
# 设计约定（改脚本时请保持）：
#   1. 缺东西时打印 "MISSING_INPUT: <标签> <路径或说明>" 并以 2 退出，
#      不要用 error 抛出无上下文的堆栈。
#   2. 版本不符只告警不中断，用 -expect_version 指定期望值。
#   3. 所有可改项都能用 -tclargs 覆盖，默认值只是某次的取值，不是前提。
##############################################################################

namespace eval ::portable {
    variable opt
    variable missing
}

proc ::portable::parse_args {argv_list} {
    variable opt
    variable missing
    array unset opt
    array set opt {}
    set i 0
    while {$i < [llength $argv_list]} {
        set key [lindex $argv_list $i]
        if {[string index $key 0] ne "-"} {
            incr i
            continue
        }
        set name [string range $key 1 end]
        if {$i + 1 < [llength $argv_list] && [string index [lindex $argv_list [expr {$i + 1}]] 0] ne "-"} {
            set opt($name) [lindex $argv_list [expr {$i + 1}]]
            incr i 2
        } else {
            set opt($name) 1
            incr i
        }
    }
    set missing {}
    return [array get opt]
}

proc ::portable::get {name default} {
    variable opt
    if {[info exists opt($name)] && $opt($name) ne ""} {
        return $opt($name)
    }
    return $default
}

proc ::portable::need {label path} {
    variable missing
    lappend missing [list $label $path]
}

proc ::portable::require_inputs {} {
    variable missing
    if {[llength $missing] == 0} {
        return
    }
    puts "MISSING_INPUT: preconditions not met, script stopped"
    foreach item $missing {
        puts "  - [lindex $item 0]: [lindex $item 1]"
    }
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    exit 2
}

proc ::portable::require_file {label path} {
    if {![file exists $path]} {
        ::portable::need $label $path
        return 0
    }
    return 1
}

proc ::portable::require_dir {label path} {
    if {![file isdirectory $path]} {
        ::portable::need $label $path
        return 0
    }
    return 1
}

proc ::portable::tool_info {expect_version} {
    set v [version -short]
    puts "TOOL_VERSION=$v"
    if {$expect_version ne "" && $v ne $expect_version} {
        puts "WARNING: expected tool version $expect_version, running $v; command and report format may differ"
        puts "WARNING: expected tool version $expect_version, running $v; command and report format may differ"
    }
    return $v
}

proc ::portable::summary {pairs} {
    foreach {k v} $pairs {
        puts "PARAM $k=$v"
    }
}
