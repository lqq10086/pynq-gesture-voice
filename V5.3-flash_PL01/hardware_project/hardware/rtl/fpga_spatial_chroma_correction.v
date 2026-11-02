`timescale 1ns/1ps

// Frame-atomic, measurement-derived spatial chroma correction for RGB565.
// R and B use independent 11x9 Q12 gain grids with 64-pixel bilinear spacing;
// G is unchanged. The bypass output preserves the original RGB565 word exactly.
module fpga_spatial_chroma_correction #(
    // 1 locks the production path to correction, including after reset.
    parameter FORCE_CORRECTION = 0,
    parameter GAIN_R_FILE = "fpga_spatial_gain_r.mem",
    parameter GAIN_B_FILE = "fpga_spatial_gain_b.mem"
) (
    input  wire        clk,
    input  wire        rst,
    // Already synchronized to clk by fpga_color_pipeline. Both correction
    // stages sample this same level at start-of-frame for an atomic A/B swap.
    input  wire        profile_request,

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
    localparam [15:0] Q12_ONE = 16'd4096;

    reg active_mode;
    wire effective_request = FORCE_CORRECTION ? 1'b1 : profile_request;

    // Four asynchronous reads per channel are intentional. The tables total
    // only 2,376 bits and synthesize as small distributed ROMs.
    (* rom_style = "distributed" *) reg [15:0] gain_r_lut [0:98];
    (* rom_style = "distributed" *) reg [15:0] gain_b_lut [0:98];
    initial begin
        $readmemh(GAIN_R_FILE, gain_r_lut);
        $readmemh(GAIN_B_FILE, gain_b_lut);
    end

    function [15:0] interpolate_64_q12;
        input [15:0] value0;
        input [15:0] value1;
        input [5:0] fraction;
        integer delta;
        integer product;
        integer rounded;
        integer result;
        begin
            delta = $signed({1'b0, value1}) - $signed({1'b0, value0});
            product = delta * fraction;
            if (product >= 0)
                rounded = (product + 32) >>> 6;
            else
                rounded = -(((-product) + 32) >>> 6);
            result = value0 + rounded;
            if (result < 0)
                interpolate_64_q12 = 16'd0;
            else if (result > 65535)
                interpolate_64_q12 = 16'hFFFF;
            else
                interpolate_64_q12 = result[15:0];
        end
    endfunction

    function [11:0] scale_q12;
        input [11:0] value;
        input [15:0] gain_q12;
        reg [31:0] product;
        reg [19:0] scaled;
        begin
            product = value * gain_q12;
            scaled = (product + 32'd2048) >> 12;
            if (scaled > 20'd4095)
                scale_q12 = 12'd4095;
            else
                scale_q12 = scaled[11:0];
        end
    endfunction

    // Static 8x8 Bayer ranks. Ordered spatial dithering converts the visible
    // RGB565 iso-gain contours into a fine, frame-stable texture without
    // temporal flicker. R and B share the rank to avoid chroma speckle.
    function [5:0] bayer8_rank;
        input [2:0] x;
        input [2:0] y;
        begin
            case ({y, x})
                6'd0: bayer8_rank = 6'd0;   6'd1: bayer8_rank = 6'd32;
                6'd2: bayer8_rank = 6'd8;   6'd3: bayer8_rank = 6'd40;
                6'd4: bayer8_rank = 6'd2;   6'd5: bayer8_rank = 6'd34;
                6'd6: bayer8_rank = 6'd10;  6'd7: bayer8_rank = 6'd42;
                6'd8: bayer8_rank = 6'd48;  6'd9: bayer8_rank = 6'd16;
                6'd10: bayer8_rank = 6'd56; 6'd11: bayer8_rank = 6'd24;
                6'd12: bayer8_rank = 6'd50; 6'd13: bayer8_rank = 6'd18;
                6'd14: bayer8_rank = 6'd58; 6'd15: bayer8_rank = 6'd26;
                6'd16: bayer8_rank = 6'd12; 6'd17: bayer8_rank = 6'd44;
                6'd18: bayer8_rank = 6'd4;  6'd19: bayer8_rank = 6'd36;
                6'd20: bayer8_rank = 6'd14; 6'd21: bayer8_rank = 6'd46;
                6'd22: bayer8_rank = 6'd6;  6'd23: bayer8_rank = 6'd38;
                6'd24: bayer8_rank = 6'd60; 6'd25: bayer8_rank = 6'd28;
                6'd26: bayer8_rank = 6'd52; 6'd27: bayer8_rank = 6'd20;
                6'd28: bayer8_rank = 6'd62; 6'd29: bayer8_rank = 6'd30;
                6'd30: bayer8_rank = 6'd54; 6'd31: bayer8_rank = 6'd22;
                6'd32: bayer8_rank = 6'd3;  6'd33: bayer8_rank = 6'd35;
                6'd34: bayer8_rank = 6'd11; 6'd35: bayer8_rank = 6'd43;
                6'd36: bayer8_rank = 6'd1;  6'd37: bayer8_rank = 6'd33;
                6'd38: bayer8_rank = 6'd9;  6'd39: bayer8_rank = 6'd41;
                6'd40: bayer8_rank = 6'd51; 6'd41: bayer8_rank = 6'd19;
                6'd42: bayer8_rank = 6'd59; 6'd43: bayer8_rank = 6'd27;
                6'd44: bayer8_rank = 6'd49; 6'd45: bayer8_rank = 6'd17;
                6'd46: bayer8_rank = 6'd57; 6'd47: bayer8_rank = 6'd25;
                6'd48: bayer8_rank = 6'd15; 6'd49: bayer8_rank = 6'd47;
                6'd50: bayer8_rank = 6'd7;  6'd51: bayer8_rank = 6'd39;
                6'd52: bayer8_rank = 6'd13; 6'd53: bayer8_rank = 6'd45;
                6'd54: bayer8_rank = 6'd5;  6'd55: bayer8_rank = 6'd37;
                6'd56: bayer8_rank = 6'd63; 6'd57: bayer8_rank = 6'd31;
                6'd58: bayer8_rank = 6'd55; 6'd59: bayer8_rank = 6'd23;
                6'd60: bayer8_rank = 6'd61; 6'd61: bayer8_rank = 6'd29;
                6'd62: bayer8_rank = 6'd53; default: bayer8_rank = 6'd21;
            endcase
        end
    endfunction

    function [15:0] pack_rgb565;
        input [11:0] red_value;
        input [11:0] green_value;
        input [11:0] blue_value;
        input [5:0] dither_rank;
        reg [17:0] red_scaled;
        reg [18:0] green_scaled;
        reg [17:0] blue_scaled;
        reg [11:0] dither_q12;
        reg [5:0] red5;
        reg [6:0] green6;
        reg [5:0] blue5;
        begin
            dither_q12 = {dither_rank, 6'b100000};
            red_scaled = red_value * 6'd31 + dither_q12;
            green_scaled = green_value * 7'd63 + 19'd2048;
            blue_scaled = blue_value * 6'd31 + dither_q12;
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

    reg [9:0] input_x;
    reg [8:0] input_y;
    wire [9:0] sample_x = s_axis_tuser ? 10'd0 : input_x;
    wire [8:0] sample_y = s_axis_tuser ? 9'd0 : input_y;
    wire [3:0] node_x = sample_x[9:6];
    wire [3:0] node_y = sample_y[8:6];
    wire [6:0] node_index_00 = node_y * 7'd11 + node_x;
    wire [6:0] node_index_01 = node_y * 7'd11 + node_x + 1'b1;
    wire [6:0] node_index_10 = (node_y + 1'b1) * 7'd11 + node_x;
    wire [6:0] node_index_11 = (node_y + 1'b1) * 7'd11 + node_x + 1'b1;

    reg valid_s0, valid_s1, valid_s2, valid_s3, valid_s4;
    reg [15:0] original_s0, original_s1, original_s2, original_s3;
    reg [15:0] data_s4;
    reg [1:0] keep_s0, keep_s1, keep_s2, keep_s3, keep_s4;
    reg user_s0, user_s1, user_s2, user_s3, user_s4;
    reg last_s0, last_s1, last_s2, last_s3, last_s4;
    reg mode_s0, mode_s1, mode_s2, mode_s3;
    reg [11:0] red_s0, green_s0, blue_s0;
    reg [11:0] red_s1, green_s1, blue_s1;
    reg [11:0] red_s2, green_s2, blue_s2;
    reg [11:0] red_s3, green_s3, blue_s3;
    reg [15:0] gain_r00_s0, gain_r01_s0, gain_r10_s0, gain_r11_s0;
    reg [15:0] gain_b00_s0, gain_b01_s0, gain_b10_s0, gain_b11_s0;
    reg [15:0] gain_r_top_s1, gain_r_bottom_s1;
    reg [15:0] gain_b_top_s1, gain_b_bottom_s1;
    reg [15:0] gain_r_s2, gain_b_s2;
    reg [5:0] x_fraction_s0;
    reg [5:0] y_fraction_s0, y_fraction_s1;
    reg [5:0] dither_rank_s0, dither_rank_s1;
    reg [5:0] dither_rank_s2, dither_rank_s3;

    wire [4:0] red5_input = s_axis_tdata[15:11];
    wire [5:0] green6_input = s_axis_tdata[10:5];
    wire [4:0] blue5_input = s_axis_tdata[4:0];
    wire [11:0] red12_input = {red5_input, red5_input, red5_input[4:3]};
    wire [11:0] green12_input = {green6_input, green6_input};
    wire [11:0] blue12_input = {blue5_input, blue5_input, blue5_input[4:3]};

    wire advance = !valid_s4 || m_axis_tready;
    assign s_axis_tready = !rst && advance;
    assign m_axis_tdata = data_s4;
    assign m_axis_tkeep = keep_s4;
    assign m_axis_tvalid = valid_s4;
    assign m_axis_tuser = user_s4;
    assign m_axis_tlast = last_s4;

    always @(posedge clk) begin
        if (rst) begin
            active_mode <= FORCE_CORRECTION ? 1'b1 : 1'b0;
            input_x <= 10'd0;
            input_y <= 9'd0;
            valid_s0 <= 1'b0;
            valid_s1 <= 1'b0;
            valid_s2 <= 1'b0;
            valid_s3 <= 1'b0;
            valid_s4 <= 1'b0;
            original_s0 <= 16'd0;
            original_s1 <= 16'd0;
            original_s2 <= 16'd0;
            original_s3 <= 16'd0;
            data_s4 <= 16'd0;
            keep_s0 <= 2'd0;
            keep_s1 <= 2'd0;
            keep_s2 <= 2'd0;
            keep_s3 <= 2'd0;
            keep_s4 <= 2'd0;
            user_s0 <= 1'b0;
            user_s1 <= 1'b0;
            user_s2 <= 1'b0;
            user_s3 <= 1'b0;
            user_s4 <= 1'b0;
            last_s0 <= 1'b0;
            last_s1 <= 1'b0;
            last_s2 <= 1'b0;
            last_s3 <= 1'b0;
            last_s4 <= 1'b0;
            mode_s0 <= 1'b0;
            mode_s1 <= 1'b0;
            mode_s2 <= 1'b0;
            mode_s3 <= 1'b0;
            red_s0 <= 12'd0;
            green_s0 <= 12'd0;
            blue_s0 <= 12'd0;
            red_s1 <= 12'd0;
            green_s1 <= 12'd0;
            blue_s1 <= 12'd0;
            red_s2 <= 12'd0;
            green_s2 <= 12'd0;
            blue_s2 <= 12'd0;
            red_s3 <= 12'd0;
            green_s3 <= 12'd0;
            blue_s3 <= 12'd0;
            gain_r00_s0 <= Q12_ONE;
            gain_r01_s0 <= Q12_ONE;
            gain_r10_s0 <= Q12_ONE;
            gain_r11_s0 <= Q12_ONE;
            gain_b00_s0 <= Q12_ONE;
            gain_b01_s0 <= Q12_ONE;
            gain_b10_s0 <= Q12_ONE;
            gain_b11_s0 <= Q12_ONE;
            gain_r_top_s1 <= Q12_ONE;
            gain_r_bottom_s1 <= Q12_ONE;
            gain_b_top_s1 <= Q12_ONE;
            gain_b_bottom_s1 <= Q12_ONE;
            gain_r_s2 <= Q12_ONE;
            gain_b_s2 <= Q12_ONE;
            x_fraction_s0 <= 6'd0;
            y_fraction_s0 <= 6'd0;
            y_fraction_s1 <= 6'd0;
            dither_rank_s0 <= 6'd0;
            dither_rank_s1 <= 6'd0;
            dither_rank_s2 <= 6'd0;
            dither_rank_s3 <= 6'd0;
        end else if (advance) begin
            valid_s4 <= valid_s3;
            valid_s3 <= valid_s2;
            valid_s2 <= valid_s1;
            valid_s1 <= valid_s0;
            valid_s0 <= s_axis_tvalid;

            data_s4 <= mode_s3 ? pack_rgb565(
                                    red_s3, green_s3, blue_s3,
                                    dither_rank_s3)
                                : original_s3;
            keep_s4 <= keep_s3;
            user_s4 <= user_s3;
            last_s4 <= last_s3;

            red_s3 <= scale_q12(red_s2, gain_r_s2);
            green_s3 <= green_s2;
            blue_s3 <= scale_q12(blue_s2, gain_b_s2);
            original_s3 <= original_s2;
            keep_s3 <= keep_s2;
            user_s3 <= user_s2;
            last_s3 <= last_s2;
            mode_s3 <= mode_s2;
            dither_rank_s3 <= dither_rank_s2;

            gain_r_s2 <= interpolate_64_q12(
                gain_r_top_s1, gain_r_bottom_s1, y_fraction_s1);
            gain_b_s2 <= interpolate_64_q12(
                gain_b_top_s1, gain_b_bottom_s1, y_fraction_s1);
            red_s2 <= red_s1;
            green_s2 <= green_s1;
            blue_s2 <= blue_s1;
            original_s2 <= original_s1;
            keep_s2 <= keep_s1;
            user_s2 <= user_s1;
            last_s2 <= last_s1;
            mode_s2 <= mode_s1;
            dither_rank_s2 <= dither_rank_s1;

            gain_r_top_s1 <= interpolate_64_q12(
                gain_r00_s0, gain_r01_s0, x_fraction_s0);
            gain_r_bottom_s1 <= interpolate_64_q12(
                gain_r10_s0, gain_r11_s0, x_fraction_s0);
            gain_b_top_s1 <= interpolate_64_q12(
                gain_b00_s0, gain_b01_s0, x_fraction_s0);
            gain_b_bottom_s1 <= interpolate_64_q12(
                gain_b10_s0, gain_b11_s0, x_fraction_s0);
            y_fraction_s1 <= y_fraction_s0;
            red_s1 <= red_s0;
            green_s1 <= green_s0;
            blue_s1 <= blue_s0;
            original_s1 <= original_s0;
            keep_s1 <= keep_s0;
            user_s1 <= user_s0;
            last_s1 <= last_s0;
            mode_s1 <= mode_s0;
            dither_rank_s1 <= dither_rank_s0;

            if (s_axis_tvalid) begin
                red_s0 <= red12_input;
                green_s0 <= green12_input;
                blue_s0 <= blue12_input;
                original_s0 <= s_axis_tdata;
                keep_s0 <= s_axis_tkeep;
                user_s0 <= s_axis_tuser;
                last_s0 <= s_axis_tlast;
                mode_s0 <= s_axis_tuser ? effective_request : active_mode;
                gain_r00_s0 <= gain_r_lut[node_index_00];
                gain_r01_s0 <= gain_r_lut[node_index_01];
                gain_r10_s0 <= gain_r_lut[node_index_10];
                gain_r11_s0 <= gain_r_lut[node_index_11];
                gain_b00_s0 <= gain_b_lut[node_index_00];
                gain_b01_s0 <= gain_b_lut[node_index_01];
                gain_b10_s0 <= gain_b_lut[node_index_10];
                gain_b11_s0 <= gain_b_lut[node_index_11];
                x_fraction_s0 <= sample_x[5:0];
                y_fraction_s0 <= sample_y[5:0];
                dither_rank_s0 <= bayer8_rank(
                    sample_x[2:0], sample_y[2:0]);

                if (s_axis_tuser) begin
                    active_mode <= effective_request;
                    input_x <= s_axis_tlast ? 10'd0 : 10'd1;
                    input_y <= s_axis_tlast ? 9'd1 : 9'd0;
                end else if (s_axis_tlast) begin
                    input_x <= 10'd0;
                    input_y <= (input_y == 9'd479) ? 9'd0 : input_y + 1'b1;
                end else begin
                    input_x <= input_x + 1'b1;
                end
            end
        end
    end
endmodule
