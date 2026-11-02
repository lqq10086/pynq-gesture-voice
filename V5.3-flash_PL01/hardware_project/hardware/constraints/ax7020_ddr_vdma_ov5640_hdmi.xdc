## AX7020 50 MHz PL oscillator
set_property PACKAGE_PIN U18 [get_ports clk50]
set_property IOSTANDARD LVCMOS33 [get_ports clk50]
create_clock -name clk50 -period 20.000 [get_ports clk50]

## OV5640 expansion board installed on J11
set_property PACKAGE_PIN F17 [get_ports cam_pwdn]
set_property PACKAGE_PIN F16 [get_ports cam_flash]
set_property PACKAGE_PIN F20 [get_ports cam_pclk]
set_property PACKAGE_PIN F19 [get_ports {cam_data[7]}]
set_property PACKAGE_PIN G20 [get_ports {cam_data[6]}]
set_property PACKAGE_PIN G19 [get_ports {cam_data[5]}]
set_property PACKAGE_PIN H18 [get_ports {cam_data[4]}]
set_property PACKAGE_PIN J18 [get_ports {cam_data[3]}]
set_property PACKAGE_PIN L20 [get_ports {cam_data[2]}]
set_property PACKAGE_PIN L19 [get_ports {cam_data[1]}]
set_property PACKAGE_PIN M20 [get_ports {cam_data[0]}]
set_property PACKAGE_PIN M19 [get_ports cam_reset_n]
set_property PACKAGE_PIN K18 [get_ports cam_sda]
set_property PACKAGE_PIN K17 [get_ports cam_href]
set_property PACKAGE_PIN J19 [get_ports cam_scl]
set_property PACKAGE_PIN K19 [get_ports cam_vsync]
set_property IOSTANDARD LVCMOS33 [get_ports {cam_pwdn cam_flash cam_pclk cam_data[*] cam_reset_n cam_sda cam_href cam_scl cam_vsync}]
set_property PULLUP true [get_ports cam_sda]

## Existing serial expansion board J11, manual page 4: SW0 pin33, SW1 pin35.
set_property PACKAGE_PIN G15 [get_ports color_sw0]
set_property PACKAGE_PIN J14 [get_ports color_sw1]
set_property IOSTANDARD LVCMOS33 [get_ports {color_sw0 color_sw1}]
set_false_path -from [get_ports {color_sw0 color_sw1}] -to [get_pins -hierarchical -filter {NAME =~ */sw_meta_reg*/D}]

## J11 pin F20 is not clock capable. The route is explicitly accepted here.
set_property CLOCK_DEDICATED_ROUTE FALSE \
    [get_nets -of_objects [get_pins cam_pclk_buf_i/I]]
## V3.3: 664 MHz PLL1 / SCLK 41.5 MHz / automatic DVP 83 MHz nominal.
## 11.800 ns constrains 84.746 MHz, tighter than nominal 12.048193 ns.
## This is a timing-analysis margin, not permission to exceed sensor limits.
create_clock -name cam_pclk -period 11.800 [get_ports cam_pclk]

## Register 0x4740=0x21 changes DVP data on PCLK falling edge. The FPGA
## samples on a +90 degree generated PCLK rising edge. Retain the inherited
## 0..8 ns input budget; do not weaken it merely to obtain timing closure.
set_input_delay -clock [get_clocks cam_pclk] -clock_fall -max 8.000 \
    [get_ports {cam_data[*] cam_href cam_vsync}]
set_input_delay -clock [get_clocks cam_pclk] -clock_fall -min 0.000 \
    [get_ports {cam_data[*] cam_href cam_vsync}]

## AX7020 direct HDMI/DVI output
set_property PACKAGE_PIN N18 [get_ports hdmi_clk_p]
set_property PACKAGE_PIN P19 [get_ports hdmi_clk_n]
set_property PACKAGE_PIN V20 [get_ports {hdmi_data_p[0]}]
set_property PACKAGE_PIN W20 [get_ports {hdmi_data_n[0]}]
set_property PACKAGE_PIN T20 [get_ports {hdmi_data_p[1]}]
set_property PACKAGE_PIN U20 [get_ports {hdmi_data_n[1]}]
set_property PACKAGE_PIN N20 [get_ports {hdmi_data_p[2]}]
set_property PACKAGE_PIN P20 [get_ports {hdmi_data_n[2]}]
set_property IOSTANDARD TMDS_33 [get_ports {hdmi_clk_p hdmi_clk_n hdmi_data_p[*] hdmi_data_n[*]}]

set_property PACKAGE_PIN V16 [get_ports hdmi_out_en]
set_property IOSTANDARD LVCMOS33 [get_ports hdmi_out_en]

## PL status LEDs, active low
set_property PACKAGE_PIN M14 [get_ports {led[0]}]
set_property PACKAGE_PIN M15 [get_ports {led[1]}]
set_property PACKAGE_PIN K16 [get_ports {led[2]}]
set_property PACKAGE_PIN J16 [get_ports {led[3]}]
set_property IOSTANDARD LVCMOS33 [get_ports {led[*]}]

## Camera, HDMI oscillator and PS clocks are independent. AXIS clock
## converters are the only functional crossings between these domains.
set_clock_groups -asynchronous \
    -group [get_clocks -include_generated_clocks cam_pclk] \
    -group [get_clocks -include_generated_clocks clk50]

## The AXI GPIO capture-enable level is synchronized into cam_pclk with two
## ASYNC_REG stages. Only the asynchronous launch into the first stage is cut.
set_false_path -to [get_pins {capture_enable_sync_reg[0]/D}]

## SW0 is a debounced level transferred through two ASYNC_REG pixel-clock
## stages. Cut only entry into stage 0; stage 0 -> stage 1 stays timed.
set_false_path -from [get_cells {control_bridge_i/pl_request_reg}] \
    -to [get_pins {pl_mode_i/request_sync_reg[0]/D}]
## The raw VSYNC copy used by the 50 MHz SCCB controller is synchronized.
## Its asynchronous entry is already covered by the cam_pclk/clk50 clock group.

## The AXI GPIO payload is held stable before its commit toggle changes.
## Cut only the entry to the first pixel-domain synchronizer stages and the
## pixel-to-control acknowledgment entry; all second-stage paths remain timed.
set bbox_cdc_entry [get_pins -hierarchical -filter {
    NAME =~ *bbox_i/meta0_reg*/D || NAME =~ *bbox_i/meta1_reg*/D ||
    NAME =~ *bbox_i/meta2_reg*/D || NAME =~ *bbox_i/commit_meta_reg/D ||
    NAME =~ *bbox_i/meta4_reg*/D || NAME =~ *bbox_i/meta5_reg*/D ||
    NAME =~ *bbox_i/meta6_reg*/D ||
    NAME =~ *bbox_i/ack_meta_reg/D
}]
set_false_path -to $bbox_cdc_entry

## SCCB is a slow open-drain asynchronous protocol. TMDS is timed inside the
## OSERDES primitives; LEDs and camera controls have no synchronous receiver.
set_false_path -from [get_ports {cam_sda cam_flash}]
set_false_path -to [get_ports {cam_pwdn cam_reset_n cam_scl cam_sda}]
set_false_path -to [get_ports {hdmi_clk_p hdmi_clk_n hdmi_data_p[*] hdmi_data_n[*] hdmi_out_en}]
set_false_path -to [get_ports {led[*]}]
