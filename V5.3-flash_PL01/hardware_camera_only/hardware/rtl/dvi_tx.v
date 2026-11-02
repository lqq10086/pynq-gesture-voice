`timescale 1ns/1ps

module dvi_tx (
    input  wire       pix_clk,
    input  wire       pix_clk_5x,
    input  wire       rst,
    input  wire [7:0] red,
    input  wire [7:0] green,
    input  wire [7:0] blue,
    input  wire       de,
    input  wire       hsync,
    input  wire       vsync,
    output wire       tmds_clk,
    output wire [2:0] tmds_data
);
    wire [9:0] enc_b;
    wire [9:0] enc_g;
    wire [9:0] enc_r;

    tmds_encoder enc0 (.clk(pix_clk), .rst(rst), .video(blue),
                       .de(de), .c0(hsync), .c1(vsync), .tmds(enc_b));
    tmds_encoder enc1 (.clk(pix_clk), .rst(rst), .video(green),
                       .de(de), .c0(1'b0), .c1(1'b0), .tmds(enc_g));
    tmds_encoder enc2 (.clk(pix_clk), .rst(rst), .video(red),
                       .de(de), .c0(1'b0), .c1(1'b0), .tmds(enc_r));

    serializer_10to1 ser0 (.clk_5x(pix_clk_5x), .clk_div(pix_clk), .rst(rst),
                           .data(enc_b), .serial(tmds_data[0]));
    serializer_10to1 ser1 (.clk_5x(pix_clk_5x), .clk_div(pix_clk), .rst(rst),
                           .data(enc_g), .serial(tmds_data[1]));
    serializer_10to1 ser2 (.clk_5x(pix_clk_5x), .clk_div(pix_clk), .rst(rst),
                           .data(enc_r), .serial(tmds_data[2]));
    serializer_10to1 serc (.clk_5x(pix_clk_5x), .clk_div(pix_clk), .rst(rst),
                           .data(10'b1111100000), .serial(tmds_clk));
endmodule
