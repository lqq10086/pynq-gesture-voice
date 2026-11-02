##############################################################################
# 用 Vitis classic（xsct）从 XSA 生成 platform、BSP、FSBL 与应用。
# 命令：python tools/run_tcl.py vivado/build_software_vitis_classic.tcl #         --args "-xsa <system.xsa> [-workspace <目录>] [-src_dir <源码目录>] [-proc ps7_cortexa9_0]"
# 直接调用：xsct build_software_vitis_classic.tcl ...
# 版本依赖：**只适用于 Vitis classic（2019.1 到 2023.1）**。2023.2 起 xsct 不再提供 platform 命令，
#           跑下去会在 platform create 处报 invalid command，属于流程换了不是脚本坏了：
#           改用 vitis -s 或 Vitis unified IDE 的 python 脚本。
# 缺参数打印 MISSING_INPUT 并以 2 退出。
##############################################################################
source [file join [file dirname [file normalize [info script]]] .. tools tcl_preamble.tcl]
::portable::parse_args $argv

set script_dir [file dirname [file normalize [info script]]]
set root_dir   [::portable::get root_dir [file normalize [file join $script_dir ..]]]
set workspace  [::portable::get workspace [file join $root_dir software vitis_workspace]]
set xsa        [::portable::get xsa [file join $root_dir build xsa_or_hdf system.xsa]]
set src_dir    [::portable::get src_dir [file join $root_dir software src]]
set elf_dir    [::portable::get elf_dir [file join $root_dir build elf]]
set proc_name  [::portable::get proc ps7_cortexa9_0]
set platform_name [::portable::get platform platform_ax7020]

::portable::require_file "XSA" $xsa
::portable::require_dir "src_dir" $src_dir
if {[catch {info commands platform} _] == 0 && [llength [info commands platform]] == 0} {
    puts "TOOL_INCOMPATIBLE: this xsct has no platform command (Vitis classic flow, <=2023.1)"
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    puts "NEED: fill every line above and rerun; these paths must come from the caller, the script does not guess"
    exit 3
}
::portable::require_inputs
set script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $script_dir ..]]
set workspace [file join $root_dir software vitis_workspace]
set xsa [file join $root_dir build xsa_or_hdf system.xsa]
set src_dir [file join $root_dir software src]
set elf_dir [file join $root_dir build elf]

# Preserve any developer edits in a previously generated Vitis workspace.
# This is always the exact derived directory under this project's software/.
if {[file dirname $workspace] ne [file join $root_dir software]} {
    error "Unexpected Vitis workspace location: $workspace"
}
if {[file exists $workspace]} {
    set previous "${workspace}_previous_[clock format [clock seconds] -format %Y%m%d_%H%M%S]"
    if {[file exists $previous]} { error "Workspace backup already exists: $previous" }
    file rename $workspace $previous
    puts "PREVIOUS_WORKSPACE_PRESERVED=$previous"
}
file mkdir $workspace
file mkdir $elf_dir
setws $workspace

platform create -name platform_ax7020 -hw $xsa -proc ps7_cortexa9_0 \
    -os standalone -arch 32-bit -out $workspace
platform active platform_ax7020
puts "GENERATING_PLATFORM_BEFORE_APPLICATION"
# Generate the official standalone BSP and FSBL from this build's XSA.
platform generate
puts "PLATFORM_GENERATED"
# Use the same vendor GCC flags as the validated Release build, without
# depending on the Eclipse app-create service (which can stall on Windows).
source [file join $script_dir build_application_cli.tcl]
