set bd_script_dir [file dirname [file normalize [info script]]]
set root_dir [file normalize [file join $bd_script_dir .. ..]]

if {[llength [get_files -quiet system.bd]]} {
 open_bd_design [get_files system.bd]
 if {[llength [get_bd_cells -quiet]]} {error "Existing BD is not empty; do not recreate"}
} else {create_bd_design system}

set DDR [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:ddrx_rtl:1.0 DDR]
set FIXED_IO [create_bd_intf_port -mode Master -vlnv xilinx.com:display_processing_system7:fixedio_rtl:1.0 FIXED_IO]

set S_AXIS_CAM [create_bd_intf_port -mode Slave -vlnv xilinx.com:interface:axis_rtl:1.0 S_AXIS_CAM]
set_property -dict [list \
    CONFIG.TDATA_NUM_BYTES {2} \
    CONFIG.HAS_TKEEP {1} \
    CONFIG.HAS_TLAST {1} \
    CONFIG.TUSER_WIDTH {1}] $S_AXIS_CAM

set M_AXIS_VIDEO [create_bd_intf_port -mode Master -vlnv xilinx.com:interface:axis_rtl:1.0 M_AXIS_VIDEO]

set cam_axis_aclk [create_bd_port -dir I -type clk -freq_hz 83000000 cam_axis_aclk]
set_property -dict [list CONFIG.FREQ_HZ {83000000} CONFIG.ASSOCIATED_BUSIF {S_AXIS_CAM} CONFIG.ASSOCIATED_RESET {cam_axis_aresetn}] $cam_axis_aclk
set cam_axis_aresetn [create_bd_port -dir I -type rst cam_axis_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $cam_axis_aresetn
set video_axis_aclk [create_bd_port -dir I -type clk -freq_hz 74375000 video_axis_aclk]
set_property -dict [list CONFIG.FREQ_HZ {74375000} CONFIG.ASSOCIATED_BUSIF {M_AXIS_VIDEO} CONFIG.ASSOCIATED_RESET {video_axis_aresetn}] $video_axis_aclk
set video_axis_aresetn [create_bd_port -dir I -type rst video_axis_aresetn]
set_property CONFIG.POLARITY ACTIVE_LOW $video_axis_aresetn
set PL_STATUS [create_bd_port -dir I -from 31 -to 0 PL_STATUS]
set PL_COUNTS [create_bd_port -dir I -from 31 -to 0 PL_COUNTS]
set CAPTURE_ARM [create_bd_port -dir O CAPTURE_ARM]
set CONTROL_CLK [create_bd_port -dir O -type clk CONTROL_CLK]
set CONTROL_RESETN [create_bd_port -dir O -type rst CONTROL_RESETN]
set_property CONFIG.POLARITY ACTIVE_LOW $CONTROL_RESETN
create_bd_port -dir O -from 3 -to 0 COLOR_COMMAND
create_bd_port -dir O COLOR_SEND
create_bd_port -dir I COLOR_RECEIVED
create_bd_port -dir I -from 31 -to 0 COLOR_RESULT
create_bd_port -dir I COLOR_RESULT_VALID
create_bd_port -dir O -from 127 -to 0 WB_COMMAND
create_bd_port -dir O WB_SEND
create_bd_port -dir I WB_RECEIVED
create_bd_port -dir I -from 127 -to 0 WB_RESULT
create_bd_port -dir I WB_RESULT_VALID
create_bd_port -dir O WB_ABORT

set ps7 [create_bd_cell -type ip -vlnv xilinx.com:ip:processing_system7:5.5 processing_system7_0]
source [file join $root_dir scripts ps_config_ax7020_official_2023_1.tcl]
set_ps_config $ps7
set_property CONFIG.PCW_I2C0_PERIPHERAL_ENABLE {0} $ps7

set rst100 [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_fclk0_100m]
set rst150 [create_bd_cell -type ip -vlnv xilinx.com:ip:proc_sys_reset:5.0 rst_fclk1_142m]

# V1.5: share RAW triple buffers with hardware dynamic genlock; no CPU pixels.
# PG020: S2MM dynamic master avoids MM2S; MM2S consumes completed S2MM frames.
set vdma [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_vdma:6.3 axi_vdma_0]
set_property -dict [list \
    CONFIG.c_include_sg {0} \
    CONFIG.c_include_mm2s {1} \
    CONFIG.c_include_s2mm {1} \
    CONFIG.c_num_fstores {3} \
    CONFIG.c_m_axi_mm2s_data_width {64} \
    CONFIG.c_m_axi_s2mm_data_width {64} \
    CONFIG.c_m_axis_mm2s_tdata_width {16} \
    CONFIG.c_include_mm2s_dre {1} \
    CONFIG.c_include_s2mm_dre {1} \
    CONFIG.c_include_mm2s_sf {0} \
    CONFIG.c_include_s2mm_sf {0} \
    CONFIG.c_mm2s_linebuffer_depth {1024} \
    CONFIG.c_s2mm_linebuffer_depth {1024} \
    CONFIG.c_mm2s_max_burst_length {64} \
    CONFIG.c_s2mm_max_burst_length {64} \
    CONFIG.c_mm2s_genlock_mode {3} \
    CONFIG.c_s2mm_genlock_mode {2} \
    CONFIG.c_use_mm2s_fsync {0} \
    CONFIG.c_use_s2mm_fsync {0} \
    CONFIG.c_enable_mm2s_frm_counter {1} \
    CONFIG.c_enable_s2mm_frm_counter {1} \
    CONFIG.c_enable_s2mm_rst_out {1}] $vdma

set cam_cc [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 axis_cam_cdc]
set_property -dict [list \
    CONFIG.IS_ACLK_ASYNC {1} \
    CONFIG.SYNCHRONIZATION_STAGES {3} \
    CONFIG.TDATA_NUM_BYTES {2} \
    CONFIG.HAS_TKEEP {1} \
    CONFIG.HAS_TLAST {1} \
    CONFIG.TUSER_WIDTH {1}] $cam_cc

set video_cc [create_bd_cell -type ip -vlnv xilinx.com:ip:axis_clock_converter:1.1 axis_video_cdc]
set_property -dict [list \
    CONFIG.IS_ACLK_ASYNC {1} \
    CONFIG.SYNCHRONIZATION_STAGES {3} \
    CONFIG.TDATA_NUM_BYTES {2} \
    CONFIG.HAS_TKEEP {1} \
    CONFIG.HAS_TLAST {1} \
    CONFIG.TUSER_WIDTH {1}] $video_cc

set status_gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 video_status_gpio]
set_property -dict [list \
    CONFIG.C_ALL_INPUTS {1} \
    CONFIG.C_GPIO_WIDTH {32} \
    CONFIG.C_IS_DUAL {1} \
    CONFIG.C_ALL_INPUTS_2 {1} \
    CONFIG.C_GPIO2_WIDTH {32}] $status_gpio

set control_gpio [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_gpio:2.0 video_control_gpio]
set_property -dict [list \
    CONFIG.C_ALL_OUTPUTS {1} \
    CONFIG.C_GPIO_WIDTH {1}] $control_gpio

set ctrl_ic [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_control_interconnect]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {4}] $ctrl_ic
set color_ctrl [create_bd_cell -type module -reference camera_control_axi_v33 camera_control_0]
foreach {port pin} {STATS_CAM_RESET stats_cam_reset STATS_CAM_VSYNC stats_cam_vsync STATS_CAM_DONE stats_cam_done STATS_CAM_OVERFLOW_EVENT stats_cam_overflow_event} {
 create_bd_port -dir I $port
 connect_bd_net [get_bd_ports $port] [get_bd_pins $color_ctrl/$pin]
}
create_bd_port -dir I -from 1 -to 0 STATS_CAM_DROP_EVENTS
connect_bd_net [get_bd_ports STATS_CAM_DROP_EVENTS] [get_bd_pins $color_ctrl/stats_cam_drop_events]
connect_bd_net [get_bd_ports cam_axis_aclk] [get_bd_pins $color_ctrl/stats_cam_clk]
connect_bd_net [get_bd_pins $ps7/FCLK_CLK1] [get_bd_pins $color_ctrl/stats_mem_clk]
connect_bd_net [get_bd_pins $vdma/s2mm_prmry_reset_out_n] [get_bd_pins $color_ctrl/stats_mem_resetn]

set mem_ic [create_bd_cell -type ip -vlnv xilinx.com:ip:axi_interconnect:2.1 axi_memory_interconnect]
set_property -dict [list CONFIG.NUM_SI {2} CONFIG.NUM_MI {1} CONFIG.SYNCHRONIZATION_STAGES {2}] $mem_ic

set irq_concat [create_bd_cell -type ip -vlnv xilinx.com:ip:xlconcat:2.1 vdma_irq_concat]
set_property CONFIG.NUM_PORTS {2} $irq_concat

connect_bd_intf_net [get_bd_intf_ports DDR] [get_bd_intf_pins $ps7/DDR]
connect_bd_intf_net [get_bd_intf_ports FIXED_IO] [get_bd_intf_pins $ps7/FIXED_IO]
connect_bd_intf_net [get_bd_intf_pins $ps7/M_AXI_GP0] [get_bd_intf_pins $ctrl_ic/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M00_AXI] [get_bd_intf_pins $vdma/S_AXI_LITE]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M01_AXI] [get_bd_intf_pins $status_gpio/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M02_AXI] [get_bd_intf_pins $control_gpio/S_AXI]
connect_bd_intf_net [get_bd_intf_pins $ctrl_ic/M03_AXI] [get_bd_intf_pins $color_ctrl/S_AXI]
connect_bd_net [get_bd_pins $color_ctrl/command] [get_bd_ports COLOR_COMMAND]
connect_bd_net [get_bd_pins $color_ctrl/command_send] [get_bd_ports COLOR_SEND]
connect_bd_net [get_bd_pins $color_ctrl/command_received] [get_bd_ports COLOR_RECEIVED]
connect_bd_net [get_bd_pins $color_ctrl/result] [get_bd_ports COLOR_RESULT]
connect_bd_net [get_bd_pins $color_ctrl/result_valid] [get_bd_ports COLOR_RESULT_VALID]
connect_bd_net [get_bd_pins $color_ctrl/sensor_command] [get_bd_ports WB_COMMAND]
connect_bd_net [get_bd_pins $color_ctrl/sensor_command_send] [get_bd_ports WB_SEND]
connect_bd_net [get_bd_pins $color_ctrl/sensor_command_received] [get_bd_ports WB_RECEIVED]
connect_bd_net [get_bd_pins $color_ctrl/sensor_result] [get_bd_ports WB_RESULT]
connect_bd_net [get_bd_pins $color_ctrl/sensor_result_valid] [get_bd_ports WB_RESULT_VALID]
connect_bd_net [get_bd_pins $color_ctrl/sensor_abort] [get_bd_ports WB_ABORT]
connect_bd_intf_net [get_bd_intf_pins $vdma/M_AXI_MM2S] [get_bd_intf_pins $mem_ic/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins $vdma/M_AXI_S2MM] [get_bd_intf_pins $mem_ic/S01_AXI]
connect_bd_intf_net [get_bd_intf_pins $vdma/M_AXI_S2MM] [get_bd_intf_pins $color_ctrl/WRITE_MON]
connect_bd_intf_net [get_bd_intf_pins $mem_ic/M00_AXI] [get_bd_intf_pins $ps7/S_AXI_HP0]

connect_bd_intf_net [get_bd_intf_ports S_AXIS_CAM] [get_bd_intf_pins $cam_cc/S_AXIS]
connect_bd_intf_net [get_bd_intf_ports S_AXIS_CAM] [get_bd_intf_pins $color_ctrl/CAM_MON]
connect_bd_intf_net [get_bd_intf_pins $cam_cc/M_AXIS] [get_bd_intf_pins $vdma/S_AXIS_S2MM]
connect_bd_intf_net [get_bd_intf_pins $cam_cc/M_AXIS] [get_bd_intf_pins $color_ctrl/MEM_MON]
connect_bd_intf_net [get_bd_intf_pins $vdma/M_AXIS_MM2S] [get_bd_intf_pins $video_cc/S_AXIS]
connect_bd_intf_net [get_bd_intf_pins $video_cc/M_AXIS] [get_bd_intf_ports M_AXIS_VIDEO]
connect_bd_net [get_bd_ports PL_STATUS] [get_bd_pins $status_gpio/gpio_io_i]
connect_bd_net [get_bd_ports PL_COUNTS] [get_bd_pins $status_gpio/gpio2_io_i]
connect_bd_net [get_bd_ports CAPTURE_ARM] [get_bd_pins $control_gpio/gpio_io_o]

connect_bd_net [get_bd_ports cam_axis_aclk] [get_bd_pins $cam_cc/s_axis_aclk]
connect_bd_net [get_bd_ports cam_axis_aresetn] [get_bd_pins $cam_cc/s_axis_aresetn]
connect_bd_net [get_bd_ports video_axis_aclk] [get_bd_pins $video_cc/m_axis_aclk]
connect_bd_net [get_bd_ports video_axis_aresetn] [get_bd_pins $video_cc/m_axis_aresetn]

connect_bd_net [get_bd_pins $ps7/FCLK_CLK0] \
    [get_bd_ports CONTROL_CLK] [get_bd_pins $color_ctrl/s_axi_aclk] [get_bd_pins $ctrl_ic/M03_ACLK] \
    [get_bd_pins $ps7/M_AXI_GP0_ACLK] \
    [get_bd_pins $rst100/slowest_sync_clk] \
    [get_bd_pins $ctrl_ic/ACLK] \
    [get_bd_pins $ctrl_ic/S00_ACLK] \
    [get_bd_pins $ctrl_ic/M00_ACLK] \
    [get_bd_pins $ctrl_ic/M01_ACLK] \
    [get_bd_pins $ctrl_ic/M02_ACLK] \
    [get_bd_pins $vdma/s_axi_lite_aclk] \
    [get_bd_pins $status_gpio/s_axi_aclk] \
    [get_bd_pins $control_gpio/s_axi_aclk]

connect_bd_net [get_bd_pins $ps7/FCLK_CLK1] \
    [get_bd_pins $ps7/S_AXI_HP0_ACLK] \
    [get_bd_pins $rst150/slowest_sync_clk] \
    [get_bd_pins $mem_ic/ACLK] \
    [get_bd_pins $mem_ic/S00_ACLK] \
    [get_bd_pins $mem_ic/S01_ACLK] \
    [get_bd_pins $mem_ic/M00_ACLK] \
    [get_bd_pins $vdma/m_axi_mm2s_aclk] \
    [get_bd_pins $vdma/m_axi_s2mm_aclk] \
    [get_bd_pins $vdma/s_axis_s2mm_aclk] \
    [get_bd_pins $vdma/m_axis_mm2s_aclk] \
    [get_bd_pins $cam_cc/m_axis_aclk] \
    [get_bd_pins $video_cc/s_axis_aclk]

connect_bd_net [get_bd_pins $ps7/FCLK_RESET0_N] \
    [get_bd_pins $rst100/ext_reset_in] \
    [get_bd_pins $rst150/ext_reset_in]

connect_bd_net [get_bd_pins $rst100/interconnect_aresetn] [get_bd_pins $ctrl_ic/ARESETN]
connect_bd_net [get_bd_pins $rst100/peripheral_aresetn] \
    [get_bd_ports CONTROL_RESETN] [get_bd_pins $color_ctrl/s_axi_aresetn] [get_bd_pins $ctrl_ic/M03_ARESETN] \
    [get_bd_pins $ctrl_ic/S00_ARESETN] \
    [get_bd_pins $ctrl_ic/M00_ARESETN] \
    [get_bd_pins $ctrl_ic/M01_ARESETN] \
    [get_bd_pins $ctrl_ic/M02_ARESETN] \
    [get_bd_pins $vdma/axi_resetn] \
    [get_bd_pins $status_gpio/s_axi_aresetn] \
    [get_bd_pins $control_gpio/s_axi_aresetn]

connect_bd_net [get_bd_pins $rst150/interconnect_aresetn] [get_bd_pins $mem_ic/ARESETN]
connect_bd_net [get_bd_pins $rst150/peripheral_aresetn] \
    [get_bd_pins $mem_ic/S00_ARESETN] \
    [get_bd_pins $mem_ic/S01_ARESETN] \
    [get_bd_pins $mem_ic/M00_ARESETN] \
    [get_bd_pins $cam_cc/m_axis_aresetn] \
    [get_bd_pins $video_cc/s_axis_aresetn]

connect_bd_net [get_bd_pins $vdma/mm2s_introut] [get_bd_pins $irq_concat/In0]
connect_bd_net [get_bd_pins $vdma/s2mm_introut] [get_bd_pins $irq_concat/In1]
connect_bd_net [get_bd_pins $irq_concat/dout] [get_bd_pins $ps7/IRQ_F2P]

assign_bd_address -offset 0x43000000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces $ps7/Data] \
    [get_bd_addr_segs $vdma/S_AXI_LITE/Reg] -force
assign_bd_address -offset 0x41200000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces $ps7/Data] \
    [get_bd_addr_segs $status_gpio/S_AXI/Reg] -force
assign_bd_address -offset 0x41210000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces $ps7/Data] \
    [get_bd_addr_segs $control_gpio/S_AXI/Reg] -force
set color_segment [get_bd_addr_segs -of_objects [get_bd_intf_pins $color_ctrl/S_AXI]]
puts "V25_COLOR_ADDRESS_SEGMENTS=$color_segment"
if {[llength $color_segment] != 1} {error "Color register module must expose exactly one AXI segment"}
assign_bd_address -offset 0x41220000 -range 0x00010000 \
    -target_address_space [get_bd_addr_spaces $ps7/Data] \
    $color_segment -force
assign_bd_address -offset 0x00000000 -range 0x40000000 \
    -target_address_space [get_bd_addr_spaces $vdma/Data_MM2S] \
    [get_bd_addr_segs $ps7/S_AXI_HP0/HP0_DDR_LOWOCM] -force
assign_bd_address -offset 0x00000000 -range 0x40000000 \
    -target_address_space [get_bd_addr_spaces $vdma/Data_S2MM] \
    [get_bd_addr_segs $ps7/S_AXI_HP0/HP0_DDR_LOWOCM] -force

source [file join $root_dir hardware bd bbox_bd.tcl]
source [file join $root_dir hardware bd ui_bd.tcl]
source [file join $root_dir hardware bd dual_bbox_bd.tcl]

regenerate_bd_layout
validate_bd_design
save_bd_design
if {[info exists ::v300_bd_export_file]} {
 write_bd_tcl -force $::v300_bd_export_file
} else {
 write_bd_tcl -force [file join $root_dir hardware bd system_bd_exported.tcl]
}
