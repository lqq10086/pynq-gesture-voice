`timescale 1ns/1ps

module serializer_10to1 (
    input  wire       clk_5x,
    input  wire       clk_div,
    input  wire       rst,
    input  wire [9:0] data,
    output wire       serial
);
    wire shift1;
    wire shift2;

    OSERDESE2 #(
        .DATA_RATE_OQ("DDR"),
        .DATA_RATE_TQ("SDR"),
        .DATA_WIDTH(10),
        .SERDES_MODE("MASTER"),
        .TRISTATE_WIDTH(1)
    ) master_i (
        .OQ(serial),
        .CLK(clk_5x),
        .CLKDIV(clk_div),
        .D1(data[0]), .D2(data[1]), .D3(data[2]), .D4(data[3]),
        .D5(data[4]), .D6(data[5]), .D7(data[6]), .D8(data[7]),
        .OCE(1'b1),
        .RST(rst),
        .SHIFTIN1(shift1),
        .SHIFTIN2(shift2),
        .T1(1'b0), .T2(1'b0), .T3(1'b0), .T4(1'b0),
        .TBYTEIN(1'b0), .TCE(1'b0),
        .OFB(), .TFB(), .TBYTEOUT(), .TQ(),
        .SHIFTOUT1(), .SHIFTOUT2()
    );

    OSERDESE2 #(
        .DATA_RATE_OQ("DDR"),
        .DATA_RATE_TQ("SDR"),
        .DATA_WIDTH(10),
        .SERDES_MODE("SLAVE"),
        .TRISTATE_WIDTH(1)
    ) slave_i (
        .OQ(),
        .CLK(clk_5x),
        .CLKDIV(clk_div),
        .D1(1'b0), .D2(1'b0), .D3(data[8]), .D4(data[9]),
        .D5(1'b0), .D6(1'b0), .D7(1'b0), .D8(1'b0),
        .OCE(1'b1),
        .RST(rst),
        .SHIFTIN1(1'b0),
        .SHIFTIN2(1'b0),
        .SHIFTOUT1(shift1),
        .SHIFTOUT2(shift2),
        .T1(1'b0), .T2(1'b0), .T3(1'b0), .T4(1'b0),
        .TBYTEIN(1'b0), .TCE(1'b0),
        .OFB(), .TFB(), .TBYTEOUT(), .TQ()
    );
endmodule
