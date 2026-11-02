# Independent immutable UI coefficient plane. Camera HP0 and AI HP1 unchanged.
set_property -dict [list CONFIG.PCW_USE_S_AXI_HP2 {1} CONFIG.PCW_S_AXI_HP2_DATA_WIDTH {64}] $ps7
set_property CONFIG.NUM_MI {10} $ctrl_ic
set ui_vdma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vdma:6.3 ui_vdma]
set_property -dict [list CONFIG.c_include_sg {0} CONFIG.c_include_s2mm {0} \
 CONFIG.c_include_mm2s {1} CONFIG.c_num_fstores {3} \
 CONFIG.c_m_axi_mm2s_data_width {64} CONFIG.c_m_axis_mm2s_tdata_width {64} \
 CONFIG.c_include_mm2s_dre {0} CONFIG.c_include_mm2s_sf {0} \
 CONFIG.c_mm2s_linebuffer_depth {4096} CONFIG.c_mm2s_max_burst_length {64} \
 CONFIG.c_mm2s_genlock_mode {0} CONFIG.c_use_mm2s_fsync {0} \
 CONFIG.c_enable_mm2s_frm_counter {1}] $ui_vdma
set ui_mem [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_ui_memory]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $ui_mem
set ui_cc [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 axis_ui_cdc]
set_property -dict [list CONFIG.IS_ACLK_ASYNC {1} CONFIG.SYNCHRONIZATION_STAGES {3} \
 CONFIG.TDATA_NUM_BYTES {8} CONFIG.HAS_TKEEP {1} CONFIG.HAS_TLAST {1} CONFIG.TUSER_WIDTH {1}] $ui_cc
set ui_fifo [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_data_fifo:2.0 axis_ui_fifo]
set_property -dict [list CONFIG.TDATA_NUM_BYTES {8} CONFIG.HAS_TKEEP {1} \
 CONFIG.HAS_TLAST {1} CONFIG.TUSER_WIDTH {1} CONFIG.FIFO_DEPTH {4096} CONFIG.FIFO_MODE {1}] $ui_fifo
create_bd_intf_port -mode Master -vlnv xilinx.com:interface:axis_rtl:1.0 M_AXIS_UI
set_property CONFIG.ASSOCIATED_BUSIF {M_AXIS_VIDEO:M_AXIS_UI} [get_bd_ports video_axis_aclk]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M07_AXI] [get_bd_intf_pins $ui_vdma/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins $ui_vdma/M_AXI_MM2S] [get_bd_intf_pins $ui_mem/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $ui_mem/M00_AXI] [get_bd_intf_pins $ps7/S_AXI_HP2]
connect_bd_intf_net [get_bd_intf_pins $ui_vdma/M_AXIS_MM2S] [get_bd_intf_pins $ui_fifo/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins $ui_fifo/M_AXIS] [get_bd_intf_pins $ui_cc/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins $ui_cc/M_AXIS] [get_bd_intf_ports M_AXIS_UI]
foreach p [list $ps7/S_AXI_HP2_ACLK $ui_mem/ACLK $ui_mem/S00_ACLK $ui_mem/M00_ACLK \
 $ui_vdma/m_axi_mm2s_aclk $ui_vdma/m_axis_mm2s_aclk $ui_fifo/s_axis_aclk $ui_cc/s_axis_aclk] {
 connect_bd_net [get_bd_pins $ps7/FCLK_CLK1] [get_bd_pins $p]
}
foreach p [list $ctrl_ic/M07_ACLK $ui_vdma/s_axi_lite_aclk] {
 connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $p]
}
connect_bd_net [get_bd_pins $rst150/interconnect_aresetn] [get_bd_pins $ui_mem/ARESETN]
foreach p [list $ui_mem/S00_ARESETN $ui_mem/M00_ARESETN $ui_fifo/s_axis_aresetn $ui_cc/s_axis_aresetn] {
 connect_bd_net [get_bd_pins $rst150/peripheral_aresetn] [get_bd_pins $p]
}
foreach p [list $ctrl_ic/M07_ARESETN $ui_vdma/axi_resetn] {
 connect_bd_net [get_bd_pins $rst100/peripheral_aresetn] [get_bd_pins $p]
}
connect_bd_net [get_bd_ports video_axis_aclk] [get_bd_pins $ui_cc/m_axis_aclk]
connect_bd_net [get_bd_ports video_axis_aresetn] [get_bd_pins $ui_cc/m_axis_aresetn]
assign_bd_address -offset 0x43010000 -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $ui_vdma/S_AXI_LITE/Reg] -force
assign_bd_address -offset 0x00000000 -range 0x40000000 -target_address_space [get_bd_addr_spaces $ui_vdma/Data_MM2S] [get_bd_addr_segs $ps7/S_AXI_HP2/HP2_DDR_LOWOCM] -force
foreach {index mi offset} {0 8 0x41290000 1 9 0x412A0000} {
 set mi [format %02d $mi]
 set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 ui_status_gpio$index]
 set_property -dict [list CONFIG.C_ALL_INPUTS {1} CONFIG.C_GPIO_WIDTH {32} CONFIG.C_IS_DUAL {1} CONFIG.C_ALL_INPUTS_2 {1} CONFIG.C_GPIO2_WIDTH {32}] $gpio
 foreach {word pin} [list [expr {$index*2}] gpio_io_i [expr {$index*2+1}] gpio2_io_i] {
  create_bd_port -dir I -from 31 -to 0 UI_STATUS$word
  connect_bd_net [get_bd_ports UI_STATUS$word] [get_bd_pins $gpio/$pin]
 }
 connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M${mi}_AXI] [get_bd_intf_pins $gpio/S_AXI]
 foreach p [list $ctrl_ic/M${mi}_ACLK $gpio/s_axi_aclk] {connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $p]}
 foreach p [list $ctrl_ic/M${mi}_ARESETN $gpio/s_axi_aresetn] {connect_bd_net [get_bd_pins $rst100/peripheral_aresetn] [get_bd_pins $p]}
 assign_bd_address -offset $offset -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $gpio/S_AXI/Reg] -force
}
