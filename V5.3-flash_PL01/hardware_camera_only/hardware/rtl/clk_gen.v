`timescale 1ns/1ps

module clk_gen (
    input  wire clk50,
    output wire pix_clk,
    output wire pix_clk_5x,
    output wire locked
);
    wire clkfb;
    wire pix_i;
    wire pix5_i;

    // 50 MHz input -> 743.75 MHz VCO -> 74.375 MHz pixel and
    // 371.875 MHz 5x serializer clock. The ratio is exactly 5:1.
    MMCME2_BASE #(
        .BANDWIDTH("OPTIMIZED"),
        .CLKIN1_PERIOD(20.0),
        .DIVCLK_DIVIDE(2),
        .CLKFBOUT_MULT_F(29.75),
        .CLKOUT0_DIVIDE_F(2.0),
        .CLKOUT1_DIVIDE(10),
        .STARTUP_WAIT("FALSE")
    ) mmcm_i (
        .CLKIN1(clk50),
        .RST(1'b0),
        .PWRDWN(1'b0),
        .CLKFBIN(clkfb),
        .CLKFBOUT(clkfb),
        .CLKFBOUTB(),
        .CLKOUT0(pix5_i),
        .CLKOUT1(pix_i),
        .LOCKED(locked),
        .CLKOUT0B(), .CLKOUT1B(), .CLKOUT2(), .CLKOUT2B(),
        .CLKOUT3(), .CLKOUT3B(), .CLKOUT4(), .CLKOUT5(), .CLKOUT6()
    );

    BUFG pix_buf  (.I(pix_i),  .O(pix_clk));
    BUFG pix5_buf (.I(pix5_i), .O(pix_clk_5x));
endmodule
