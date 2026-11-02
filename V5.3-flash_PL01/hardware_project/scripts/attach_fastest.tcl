# Add one newly qualified Fastest core; camera/UI/dual-slot source is untouched.
set flash_ps [get_bd_cells processing_system7_0]
set flash_ctrl [get_bd_cells axi_control_interconnect]
set flash_rst [get_bd_cells rst_fclk0_100m]
if {[get_property CONFIG.NUM_MI $flash_ctrl] != 12} {error "Unexpected camera-only control topology"}
if {[llength [get_bd_cells -quiet -hier -filter {VLNV =~ *:conv_accel:* || VLNV =~ *:fastest_accel:*}]] != 0} {error "Neural core unexpectedly present before attach"}
set defs [get_ipdefs -all -filter {VLNV =~ *:fastest_accel:1.0}]
if {[llength $defs] != 1} {error "Exactly one newly exported fastest_accel:1.0 IP is required"}
set flash_core [create_bd_cell -type ip -vlnv [get_property VLNV $defs] fastest_accel_0]
set_property -dict [list CONFIG.PCW_USE_S_AXI_HP1 {1} CONFIG.PCW_S_AXI_HP1_DATA_WIDTH {64}] $flash_ps
set_property CONFIG.NUM_MI 14 $flash_ctrl
connect_bd_intf_net [get_bd_intf_pins $flash_ctrl/M12_AXI] [get_bd_intf_pins $flash_core/s_axi_control]
connect_bd_net [get_bd_pins $flash_ps/FCLK_CLK0] [get_bd_pins $flash_ctrl/M12_ACLK] [get_bd_pins $flash_core/ap_clk] [get_bd_pins $flash_ps/S_AXI_HP1_ACLK]
connect_bd_net [get_bd_pins $flash_rst/peripheral_aresetn] [get_bd_pins $flash_ctrl/M12_ARESETN] [get_bd_pins $flash_core/ap_rst_n]
set flash_masters [lsort [get_bd_intf_pins -of_objects $flash_core -filter {MODE == Master && VLNV =~ xilinx.com:interface:aximm_rtl:*}]]
if {[llength $flash_masters] != 1 || [file tail [lindex $flash_masters 0]] ne "m_axi_gmem"} {error "Frozen Fastest IP must expose one m_axi_gmem master"}
set flash_mem [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_flash_memory]
set_property -dict [list CONFIG.NUM_SI [llength $flash_masters] CONFIG.NUM_MI {1} CONFIG.SYNCHRONIZATION_STAGES {2}] $flash_mem
set index 0
foreach master $flash_masters {
 set tag [format S%02d $index]
 connect_bd_intf_net $master [get_bd_intf_pins $flash_mem/${tag}_AXI]
 connect_bd_net [get_bd_pins $flash_ps/FCLK_CLK0] [get_bd_pins $flash_mem/${tag}_ACLK]
 connect_bd_net [get_bd_pins $flash_rst/peripheral_aresetn] [get_bd_pins $flash_mem/${tag}_ARESETN]
 incr index
}
connect_bd_intf_net [get_bd_intf_pins $flash_mem/M00_AXI] [get_bd_intf_pins $flash_ps/S_AXI_HP1]
connect_bd_net [get_bd_pins $flash_ps/FCLK_CLK0] [get_bd_pins $flash_mem/ACLK] [get_bd_pins $flash_mem/M00_ACLK]
connect_bd_net [get_bd_pins $flash_rst/interconnect_aresetn] [get_bd_pins $flash_mem/ARESETN]
connect_bd_net [get_bd_pins $flash_rst/peripheral_aresetn] [get_bd_pins $flash_mem/M00_ARESETN]
# Keep phase observable in this fresh ABI, without changing video control GPIO.
set flash_gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 flash_phase_gpio]
set_property -dict [list CONFIG.C_GPIO_WIDTH {32} CONFIG.C_ALL_INPUTS {1} CONFIG.C_IS_DUAL {0}] $flash_gpio
set flash_concat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 flash_phase_pad]
set_property -dict [list CONFIG.NUM_PORTS {2} CONFIG.IN0_WIDTH {3} CONFIG.IN1_WIDTH {29}] $flash_concat
set flash_zero [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconstant:1.1 flash_zero29]
set_property -dict [list CONFIG.CONST_WIDTH {29} CONFIG.CONST_VAL {0}] $flash_zero
connect_bd_net [get_bd_pins $flash_core/phase] [get_bd_pins $flash_concat/In0]
connect_bd_net [get_bd_pins $flash_zero/dout] [get_bd_pins $flash_concat/In1]
connect_bd_net [get_bd_pins $flash_concat/dout] [get_bd_pins $flash_gpio/gpio_io_i]
connect_bd_intf_net [get_bd_intf_pins $flash_ctrl/M13_AXI] [get_bd_intf_pins $flash_gpio/S_AXI]
connect_bd_net [get_bd_pins $flash_ps/FCLK_CLK0] [get_bd_pins $flash_ctrl/M13_ACLK] [get_bd_pins $flash_gpio/s_axi_aclk]
connect_bd_net [get_bd_pins $flash_rst/peripheral_aresetn] [get_bd_pins $flash_ctrl/M13_ARESETN] [get_bd_pins $flash_gpio/s_axi_aresetn]
set flash_segment [get_bd_addr_segs -of_objects [get_bd_intf_pins $flash_core/s_axi_control]]
if {[llength $flash_segment] != 1} {error "Expected one Fastest AXI-Lite register segment"}
assign_bd_address -offset 0x43C00000 -range 0x00010000 -target_address_space [get_bd_addr_spaces $flash_ps/Data] $flash_segment -force
assign_bd_address -offset 0x412D0000 -range 0x00010000 -target_address_space [get_bd_addr_spaces $flash_ps/Data] [get_bd_addr_segs $flash_gpio/S_AXI/Reg] -force
set flash_address_space [get_bd_addr_spaces $flash_core/Data_m_axi_gmem]
if {[llength $flash_address_space] != 1} {error "Frozen Fastest DDR master address space is missing"}
assign_bd_address -offset 0x00000000 -range 0x40000000 -target_address_space $flash_address_space [get_bd_addr_segs $flash_ps/S_AXI_HP1/HP1_DDR_LOWOCM] -force
regenerate_bd_layout
validate_bd_design
save_bd_design
puts "PL01_FASTEST_BD_ATTACHED_REQUIRES_FULL_ELABORATION_AND_ROUTED_QUALIFICATION"
