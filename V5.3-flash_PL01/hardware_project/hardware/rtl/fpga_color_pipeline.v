`timescale 1ns/1ps
`include "fpga_color_profile.vh"

// Global, frame-atomic RGB565 color pipeline for the post-lens-correction
// MM2S-to-HDMI path.  The bypass path carries the original AXIS word exactly;
// the calibrated path expands to 12 bits/channel and stays at that precision
// through gain/offset, signed Q3.12 CCM, saturation and 1D gamma processing.
module fpga_color_pipeline #(
    // Production builds set this to 1. Bypass remains available only to
    // reusable-module tests; even the first post-reset frame is corrected.
    parameter FORCE_CORRECTION = 0,
    parameter signed [15:0] GAIN_R_Q12 = `FPGA_COLOR_GAIN_R_Q12,
    parameter signed [15:0] GAIN_G_Q12 = `FPGA_COLOR_GAIN_G_Q12,
    parameter signed [15:0] GAIN_B_Q12 = `FPGA_COLOR_GAIN_B_Q12,
    parameter signed [15:0] OFFSET_R = `FPGA_COLOR_OFFSET_R,
    parameter signed [15:0] OFFSET_G = `FPGA_COLOR_OFFSET_G,
    parameter signed [15:0] OFFSET_B = `FPGA_COLOR_OFFSET_B,
    parameter signed [15:0] CCM_RR_Q12 = `FPGA_COLOR_CCM_RR_Q12,
    parameter signed [15:0] CCM_RG_Q12 = `FPGA_COLOR_CCM_RG_Q12,
    parameter signed [15:0] CCM_RB_Q12 = `FPGA_COLOR_CCM_RB_Q12,
    parameter signed [15:0] CCM_GR_Q12 = `FPGA_COLOR_CCM_GR_Q12,
    parameter signed [15:0] CCM_GG_Q12 = `FPGA_COLOR_CCM_GG_Q12,
    parameter signed [15:0] CCM_GB_Q12 = `FPGA_COLOR_CCM_GB_Q12,
    parameter signed [15:0] CCM_BR_Q12 = `FPGA_COLOR_CCM_BR_Q12,
    parameter signed [15:0] CCM_BG_Q12 = `FPGA_COLOR_CCM_BG_Q12,
    parameter signed [15:0] CCM_BB_Q12 = `FPGA_COLOR_CCM_BB_Q12,
    parameter signed [15:0] SATURATION_Q12 = `FPGA_COLOR_SATURATION_Q12,
    parameter GAMMA_R_FILE = "fpga_color_gamma_r.mem",
    parameter GAMMA_G_FILE = "fpga_color_gamma_g.mem",
    parameter GAMMA_B_FILE = "fpga_color_gamma_b.mem"
) (
    input  wire        clk,
    input  wire        rst,
    input  wire        profile_request_async,
    output wire        profile_request,
    output wire        profile_active,
    output wire        profile_pending,

    input wire [15:0] s_axis_tag,
    output wire [15:0] m_axis_tag,
    input  wire [15:0] s_axis_tdata,
    input  wire [1:0]  s_axis_tkeep,
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    input  wire        s_axis_tuser,
    input  wire        s_axis_tlast,

    output wire [15:0] m_axis_tdata,
    output wire [1:0]  m_axis_tkeep,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tuser,
    output wire        m_axis_tlast
);
    (* ASYNC_REG = "TRUE" *) reg request_meta;
    (* ASYNC_REG = "TRUE" *) reg request_sync;
    reg active_mode;
    reg pending_mode;
    wire effective_request = FORCE_CORRECTION ? 1'b1 : request_sync;

    (* rom_style = "block" *) reg [11:0] gamma_r_lut [0:4095];
    (* rom_style = "block" *) reg [11:0] gamma_g_lut [0:4095];
    (* rom_style = "block" *) reg [11:0] gamma_b_lut [0:4095];

    initial begin
        $readmemh(GAMMA_R_FILE, gamma_r_lut);
        $readmemh(GAMMA_G_FILE, gamma_g_lut);
        $readmemh(GAMMA_B_FILE, gamma_b_lut);
    end

    function signed [23:0] round_shift_q12;
        input signed [47:0] value;
        reg signed [47:0] magnitude;
        begin
            if (value >= 0)
                round_shift_q12 = (value + 48'sd2048) >>> 12;
            else begin
                magnitude = -value;
                round_shift_q12 = -((magnitude + 48'sd2048) >>> 12);
            end
        end
    endfunction

    function signed [23:0] round_shift_q8;
        input signed [47:0] value;
        reg signed [47:0] magnitude;
        begin
            if (value >= 0)
                round_shift_q8 = (value + 48'sd128) >>> 8;
            else begin
                magnitude = -value;
                round_shift_q8 = -((magnitude + 48'sd128) >>> 8);
            end
        end
    endfunction

    function signed [23:0] apply_gain_offset;
        input [11:0] value;
        input signed [15:0] gain_q12;
        input signed [15:0] offset_value;
        reg signed [47:0] product;
        begin
            product = $signed({1'b0, value}) * gain_q12;
            apply_gain_offset = round_shift_q12(product) + offset_value;
        end
    endfunction

    function signed [23:0] apply_ccm_row;
        input signed [23:0] red_value;
        input signed [23:0] green_value;
        input signed [23:0] blue_value;
        input signed [15:0] coeff_r;
        input signed [15:0] coeff_g;
        input signed [15:0] coeff_b;
        reg signed [47:0] sum;
        begin
            sum = red_value * coeff_r +
                  green_value * coeff_g +
                  blue_value * coeff_b;
            apply_ccm_row = round_shift_q12(sum);
        end
    endfunction

    function signed [23:0] calculate_luma;
        input signed [23:0] red_value;
        input signed [23:0] green_value;
        input signed [23:0] blue_value;
        reg signed [47:0] sum;
        begin
            sum = red_value * 16'sd77 +
                  green_value * 16'sd150 +
                  blue_value * 16'sd29;
            calculate_luma = round_shift_q8(sum);
        end
    endfunction

    function signed [23:0] apply_saturation;
        input signed [23:0] channel_value;
        input signed [23:0] luma_value;
        input signed [15:0] saturation;
        reg signed [47:0] product;
        begin
            product = (channel_value - luma_value) * saturation;
            apply_saturation = luma_value + round_shift_q12(product);
        end
    endfunction

    function [11:0] clamp12;
        input signed [23:0] value;
        begin
            if (value < 0)
                clamp12 = 12'd0;
            else if (value > 24'sd4095)
                clamp12 = 12'd4095;
            else
                clamp12 = value[11:0];
        end
    endfunction

    function [15:0] pack_rgb565;
        input [11:0] red_value;
        input [11:0] green_value;
        input [11:0] blue_value;
        reg [17:0] red_scaled;
        reg [18:0] green_scaled;
        reg [17:0] blue_scaled;
        reg [5:0] red5;
        reg [6:0] green6;
        reg [5:0] blue5;
        begin
            red_scaled = red_value * 6'd31 + 18'd2048;
            green_scaled = green_value * 7'd63 + 19'd2048;
            blue_scaled = blue_value * 6'd31 + 18'd2048;
            red5 = red_scaled >> 12;
            green6 = green_scaled >> 12;
            blue5 = blue_scaled >> 12;
            pack_rgb565 = {
                (red5 > 31) ? 5'd31 : red5[4:0],
                (green6 > 63) ? 6'd63 : green6[5:0],
                (blue5 > 31) ? 5'd31 : blue5[4:0]
            };
        end
    endfunction

    reg [15:0] tag_s0,tag_s1,tag_s2,tag_s3,tag_s4,tag_s5,tag_s6;
    assign m_axis_tag=tag_s6;
    always @(posedge clk) begin
        if(rst) begin
            tag_s0<=0;tag_s1<=0;tag_s2<=0;tag_s3<=0;tag_s4<=0;tag_s5<=0;tag_s6<=0;
        end else if(advance) begin
            tag_s0<=s_axis_tag;tag_s1<=tag_s0;tag_s2<=tag_s1;tag_s3<=tag_s2;
            tag_s4<=tag_s3;tag_s5<=tag_s4;tag_s6<=tag_s5;
        end
    end
    reg valid_s0, valid_s1, valid_s2, valid_s3, valid_s4, valid_s5, valid_s6;
    reg [15:0] original_s0, original_s1, original_s2;
    reg [15:0] original_s3, original_s4, original_s5, data_s6;
    reg [1:0] keep_s0, keep_s1, keep_s2, keep_s3, keep_s4, keep_s5, keep_s6;
    reg user_s0, user_s1, user_s2, user_s3, user_s4, user_s5, user_s6;
    reg last_s0, last_s1, last_s2, last_s3, last_s4, last_s5, last_s6;
    reg mode_s0, mode_s1, mode_s2, mode_s3, mode_s4, mode_s5;
    reg signed [23:0] red_s1, green_s1, blue_s1;
    reg signed [23:0] red_s2, green_s2, blue_s2;
    reg signed [23:0] red_s3, green_s3, blue_s3, luma_s3;
    reg [11:0] red_s4, green_s4, blue_s4;
    reg [11:0] red_s5, green_s5, blue_s5;

    wire [4:0] red5_s0 = original_s0[15:11];
    wire [5:0] green6_s0 = original_s0[10:5];
    wire [4:0] blue5_s0 = original_s0[4:0];
    wire [11:0] red12_s0 = {red5_s0, red5_s0, red5_s0[4:3]};
    wire [11:0] green12_s0 = {green6_s0, green6_s0};
    wire [11:0] blue12_s0 = {blue5_s0, blue5_s0, blue5_s0[4:3]};

    wire advance = !valid_s6 || m_axis_tready;
    assign s_axis_tready = !rst && advance;
    assign m_axis_tdata = data_s6;
    assign m_axis_tkeep = keep_s6;
    assign m_axis_tvalid = valid_s6;
    assign m_axis_tuser = user_s6;
    assign m_axis_tlast = last_s6;
    assign profile_request = effective_request;
    assign profile_active = active_mode;
    assign profile_pending = pending_mode;

    always @(posedge clk) begin
        if (rst) begin
            request_meta <= 1'b0;
            request_sync <= 1'b0;
            pending_mode <= 1'b0;
        end else begin
            request_meta <= profile_request_async;
            request_sync <= request_meta;
            // Register the status before it crosses into the AXI GPIO input
            // synchronizer. This avoids combinational logic immediately ahead
            // of a CDC boundary while preserving frame-atomic mode commits.
            pending_mode <= effective_request != active_mode;
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            active_mode <= FORCE_CORRECTION ? 1'b1 : 1'b0;
            valid_s0 <= 1'b0;
            valid_s1 <= 1'b0;
            valid_s2 <= 1'b0;
            valid_s3 <= 1'b0;
            valid_s4 <= 1'b0;
            valid_s5 <= 1'b0;
            valid_s6 <= 1'b0;
            original_s0 <= 16'd0;
            original_s1 <= 16'd0;
            original_s2 <= 16'd0;
            original_s3 <= 16'd0;
            original_s4 <= 16'd0;
            original_s5 <= 16'd0;
            data_s6 <= 16'd0;
            keep_s0 <= 2'd0;
            keep_s1 <= 2'd0;
            keep_s2 <= 2'd0;
            keep_s3 <= 2'd0;
            keep_s4 <= 2'd0;
            keep_s5 <= 2'd0;
            keep_s6 <= 2'd0;
            user_s0 <= 1'b0;
            user_s1 <= 1'b0;
            user_s2 <= 1'b0;
            user_s3 <= 1'b0;
            user_s4 <= 1'b0;
            user_s5 <= 1'b0;
            user_s6 <= 1'b0;
            last_s0 <= 1'b0;
            last_s1 <= 1'b0;
            last_s2 <= 1'b0;
            last_s3 <= 1'b0;
            last_s4 <= 1'b0;
            last_s5 <= 1'b0;
            last_s6 <= 1'b0;
            mode_s0 <= 1'b0;
            mode_s1 <= 1'b0;
            mode_s2 <= 1'b0;
            mode_s3 <= 1'b0;
            mode_s4 <= 1'b0;
            mode_s5 <= 1'b0;
            red_s1 <= 24'sd0;
            green_s1 <= 24'sd0;
            blue_s1 <= 24'sd0;
            red_s2 <= 24'sd0;
            green_s2 <= 24'sd0;
            blue_s2 <= 24'sd0;
            red_s3 <= 24'sd0;
            green_s3 <= 24'sd0;
            blue_s3 <= 24'sd0;
            luma_s3 <= 24'sd0;
            red_s4 <= 12'd0;
            green_s4 <= 12'd0;
            blue_s4 <= 12'd0;
            red_s5 <= 12'd0;
            green_s5 <= 12'd0;
            blue_s5 <= 12'd0;
        end else if (advance) begin
            valid_s6 <= valid_s5;
            valid_s5 <= valid_s4;
            valid_s4 <= valid_s3;
            valid_s3 <= valid_s2;
            valid_s2 <= valid_s1;
            valid_s1 <= valid_s0;
            valid_s0 <= s_axis_tvalid;

            data_s6 <= mode_s5 ? pack_rgb565(red_s5, green_s5, blue_s5)
                               : original_s5;
            keep_s6 <= keep_s5;
            user_s6 <= user_s5;
            last_s6 <= last_s5;

            red_s5 <= gamma_r_lut[red_s4];
            green_s5 <= gamma_g_lut[green_s4];
            blue_s5 <= gamma_b_lut[blue_s4];
            original_s5 <= original_s4;
            keep_s5 <= keep_s4;
            user_s5 <= user_s4;
            last_s5 <= last_s4;
            mode_s5 <= mode_s4;

            red_s4 <= clamp12(apply_saturation(red_s3, luma_s3, SATURATION_Q12));
            green_s4 <= clamp12(apply_saturation(green_s3, luma_s3, SATURATION_Q12));
            blue_s4 <= clamp12(apply_saturation(blue_s3, luma_s3, SATURATION_Q12));
            original_s4 <= original_s3;
            keep_s4 <= keep_s3;
            user_s4 <= user_s3;
            last_s4 <= last_s3;
            mode_s4 <= mode_s3;

            luma_s3 <= calculate_luma(red_s2, green_s2, blue_s2);
            red_s3 <= red_s2;
            green_s3 <= green_s2;
            blue_s3 <= blue_s2;
            original_s3 <= original_s2;
            keep_s3 <= keep_s2;
            user_s3 <= user_s2;
            last_s3 <= last_s2;
            mode_s3 <= mode_s2;

            red_s2 <= apply_ccm_row(
                red_s1, green_s1, blue_s1,
                CCM_RR_Q12, CCM_RG_Q12, CCM_RB_Q12);
            green_s2 <= apply_ccm_row(
                red_s1, green_s1, blue_s1,
                CCM_GR_Q12, CCM_GG_Q12, CCM_GB_Q12);
            blue_s2 <= apply_ccm_row(
                red_s1, green_s1, blue_s1,
                CCM_BR_Q12, CCM_BG_Q12, CCM_BB_Q12);
            original_s2 <= original_s1;
            keep_s2 <= keep_s1;
            user_s2 <= user_s1;
            last_s2 <= last_s1;
            mode_s2 <= mode_s1;

            red_s1 <= apply_gain_offset(red12_s0, GAIN_R_Q12, OFFSET_R);
            green_s1 <= apply_gain_offset(green12_s0, GAIN_G_Q12, OFFSET_G);
            blue_s1 <= apply_gain_offset(blue12_s0, GAIN_B_Q12, OFFSET_B);
            original_s1 <= original_s0;
            keep_s1 <= keep_s0;
            user_s1 <= user_s0;
            last_s1 <= last_s0;
            mode_s1 <= mode_s0;

            if (s_axis_tvalid) begin
                original_s0 <= s_axis_tdata;
                keep_s0 <= s_axis_tkeep;
                user_s0 <= s_axis_tuser;
                last_s0 <= s_axis_tlast;
                mode_s0 <= s_axis_tuser ? effective_request : active_mode;
                if (s_axis_tuser)
                    active_mode <= effective_request;
            end
        end
    end
endmodule
