`timescale 1ns/1ps
// Diagnostic GPIO sources only. Register combinational flags in their launch
// domains, and cross active once before duplicating it in the AXI status word.
module color_status_cdc(
    input wire clk50, rst50, pix_clk, pix_rst, axi_clk,
    input wire switch_isp, boot_isp,
    input wire pending_pix, requested_pix, active_pix,
    output reg switch_changed50 = 1'b0,
    output reg pending_status_pix = 1'b0,
    output wire active_axi
);
    always @(posedge clk50) begin
        if (rst50)
            switch_changed50 <= 1'b0;
        else
            switch_changed50 <= switch_isp != boot_isp;
    end

    always @(posedge pix_clk) begin
        if (pix_rst)
            pending_status_pix <= 1'b0;
        else
            pending_status_pix <= pending_pix || (requested_pix != active_pix);
    end

    // active_pix is already registered by fpga_color_pipeline. Both GPIO
    // copies are synchronous to CONTROL_CLK after this one crossing.
    xpm_cdc_single #(
        .DEST_SYNC_FF(3), .SRC_INPUT_REG(0),
        .INIT_SYNC_FF(1), .SIM_ASSERT_CHK(1)
    ) active_cdc_i (
        .src_clk(1'b0), .src_in(active_pix),
        .dest_clk(axi_clk), .dest_out(active_axi)
    );
endmodule
