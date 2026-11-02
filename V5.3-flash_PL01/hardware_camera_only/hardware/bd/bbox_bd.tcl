# AXI GPIO shadow words for the independent pixel-domain bounding box overlay.
# The software toggles BBOX_WORD3[0] only after all payload words are stable.
set_property CONFIG.NUM_MI {7} $ctrl_ic
foreach port {BBOX_WORD0 BBOX_WORD1 BBOX_WORD2 BBOX_WORD3} {
 create_bd_port -dir O -from 31 -to 0 $port
}
create_bd_port -dir I BBOX_ACK
set bbox0 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 bbox_shadow_gpio0]
set bbox1 [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 bbox_shadow_gpio1]
foreach gpio [list $bbox0 $bbox1] {
 set_property -dict [list CONFIG.C_ALL_OUTPUTS {1} CONFIG.C_GPIO_WIDTH {32} CONFIG.C_IS_DUAL {1} CONFIG.C_ALL_OUTPUTS_2 {1} CONFIG.C_GPIO2_WIDTH {32}] $gpio
}
set bbox_ack [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 bbox_ack_gpio]
set_property -dict [list CONFIG.C_ALL_INPUTS {1} CONFIG.C_GPIO_WIDTH {1}] $bbox_ack
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M04_AXI] [get_bd_intf_pins $bbox0/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M05_AXI] [get_bd_intf_pins $bbox1/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M06_AXI] [get_bd_intf_pins $bbox_ack/S_AXI]
connect_bd_net [get_bd_pins $bbox0/gpio_io_o] [get_bd_ports BBOX_WORD0]
connect_bd_net [get_bd_pins $bbox0/gpio2_io_o] [get_bd_ports BBOX_WORD1]
connect_bd_net [get_bd_pins $bbox1/gpio_io_o] [get_bd_ports BBOX_WORD2]
connect_bd_net [get_bd_pins $bbox1/gpio2_io_o] [get_bd_ports BBOX_WORD3]
connect_bd_net [get_bd_ports BBOX_ACK] [get_bd_pins $bbox_ack/gpio_io_i]
foreach p [list $ctrl_ic/M04_ACLK $ctrl_ic/M05_ACLK $ctrl_ic/M06_ACLK $bbox0/s_axi_aclk $bbox1/s_axi_aclk $bbox_ack/s_axi_aclk] {
 connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] [get_bd_pins $p]
}
foreach p [list $ctrl_ic/M04_ARESETN $ctrl_ic/M05_ARESETN $ctrl_ic/M06_ARESETN $bbox0/s_axi_aresetn $bbox1/s_axi_aresetn $bbox_ack/s_axi_aresetn] {
 connect_bd_net [get_bd_pins $rst100/peripheral_aresetn] [get_bd_pins $p]
}
assign_bd_address -offset 0x41260000 -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $bbox0/S_AXI/Reg] -force
assign_bd_address -offset 0x41270000 -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $bbox1/S_AXI/Reg] -force
assign_bd_address -offset 0x41280000 -range 0x10000 -target_address_space [get_bd_addr_spaces $ps7/Data] [get_bd_addr_segs $bbox_ack/S_AXI/Reg] -force
