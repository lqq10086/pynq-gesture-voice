# Slot1 payload; slot0 WORD3[0] commits both slots with the existing common ACK.
set_property CONFIG.NUM_MI {12} $ctrl_ic
foreach {index mi offset} {2 10 0x412B0000 3 11 0x412C0000} {
 set gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 bbox_shadow_gpio$index]
 set_property -dict [list CONFIG.C_ALL_OUTPUTS {1} CONFIG.C_GPIO_WIDTH {32} CONFIG.C_IS_DUAL {1} CONFIG.C_ALL_OUTPUTS_2 {1} CONFIG.C_GPIO2_WIDTH {32}] $gpio
 connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M${mi}_AXI] [get_bd_intf_pins $gpio/S_AXI]
 foreach p [list $ctrl_ic/M${mi}_ACLK $gpio/s_axi_aclk] {connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $p]}
 foreach p [list $ctrl_ic/M${mi}_ARESETN $gpio/s_axi_aresetn] {connect_bd_net [get_bd_pins $rst100/peripheral_aresetn] [get_bd_pins $p]}
 assign_bd_address -offset $offset -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $gpio/S_AXI/Reg] -force
}
foreach {word gpio pin} {0 2 gpio_io_o 1 2 gpio2_io_o 2 3 gpio_io_o} {
 create_bd_port -dir O -from 31 -to 0 BBOX2_WORD$word
 connect_bd_net [get_bd_pins bbox_shadow_gpio$gpio/$pin] [get_bd_ports BBOX2_WORD$word]
}
