`timescale 1ns/1ps

module axis_rgb565_video_out #(
    parameter integer COLOR_CORRECTION_ENABLE = 0,
    parameter integer H_ACTIVE = 1280,
    parameter integer H_TOTAL  = 1650,
    parameter integer V_TOTAL  = 750
) (
    input  wire        pix_clk,
    input  wire        rst,
    input wire [15:0] s_axis_tag,
    output wire [15:0] pixel_tag,
    input  wire [15:0] s_axis_tdata,
    input  wire        s_axis_tvalid,
    output wire        s_axis_tready,
    input  wire        s_axis_tuser,
    input  wire        s_axis_tlast,
    output wire [7:0]  red,
    output wire [7:0]  green,
    output wire [7:0]  blue,
    output wire        de,
    output wire        hsync,
    output wire        vsync,
    output wire        frame_start,
    output reg         video_running,
    output reg         underflow_error,
    output reg         sync_error,
    output reg  [31:0] displayed_frame_count,
    output reg  [31:0] underflow_count
);
    localparam integer GAIN_MIN_Q8 = 192;
    localparam integer GAIN_MAX_Q8 = 352;

    wire [$clog2(H_TOTAL)-1:0] x;
    wire [$clog2(V_TOTAL)-1:0] y;
    wire active;
    wire timing_hsync;
    wire timing_vsync;
    wire timing_frame_start;
    reg stream_locked;
    reg startup_armed;

    wire [7:0] raw_red   = {s_axis_tdata[15:11], s_axis_tdata[15:13]};
    wire [7:0] raw_green = {s_axis_tdata[10:5],  s_axis_tdata[10:9]};
    wire [7:0] raw_blue  = {s_axis_tdata[4:0],   s_axis_tdata[4:2]};

    wire first_pixel_candidate = timing_frame_start && s_axis_tuser;
    wire pixel_usable = active && s_axis_tvalid &&
                        (stream_locked || first_pixel_candidate);
    wire transfer = s_axis_tvalid && s_axis_tready;

    reg [15:0] tag_s1,tag_s2,tag_pipe;
    assign pixel_tag=tag_pipe;
    always @(posedge pix_clk) begin
        if(rst) begin tag_s1<=0;tag_s2<=0;tag_pipe<=0;end
        else begin
            tag_s1<=pixel_usable ? s_axis_tag : 16'd0;
            tag_s2<=tag_s1;tag_pipe<=tag_s2;
        end
    end
    reg [7:0] red_s1;
    reg [7:0] green_s1;
    reg [7:0] blue_s1;
    reg       usable_s1;
    reg       de_s1;
    reg       hsync_s1;
    reg       vsync_s1;
    reg       frame_start_s1;
    integer   red_base_s1;
    integer   red_amp_s1;
    integer   red_y_s1;
    integer   blue_base_s1;
    integer   blue_amp_s1;
    integer   blue_y_s1;

    reg [7:0] red_s2;
    reg [7:0] green_s2;
    reg [7:0] blue_s2;
    reg       usable_s2;
    reg       de_s2;
    reg       hsync_s2;
    reg       vsync_s2;
    reg       frame_start_s2;
    integer   red_gain_s2;
    integer   blue_gain_s2;

    reg [7:0] red_pipe;
    reg [7:0] green_pipe;
    reg [7:0] blue_pipe;
    reg       de_pipe;
    reg       hsync_pipe;
    reg       vsync_pipe;
    reg       frame_start_pipe;

    function integer red_base_node;
        input integer index;
        begin
            case (index)
                0: red_base_node = 13;
                1: red_base_node = 18;
                2: red_base_node = 28;
                3: red_base_node = 43;
                4: red_base_node = 52;
                5: red_base_node = 50;
                6: red_base_node = 36;
                7: red_base_node = 13;
                8: red_base_node = -11;
                9: red_base_node = -26;
                default: red_base_node = -36;
            endcase
        end
    endfunction

    function integer red_amp_node;
        input integer index;
        begin
            case (index)
                0: red_amp_node = 0;
                1: red_amp_node = 10;
                2: red_amp_node = 24;
                3: red_amp_node = 38;
                4: red_amp_node = 45;
                5: red_amp_node = 45;
                6: red_amp_node = 39;
                7: red_amp_node = 25;
                8: red_amp_node = 10;
                9: red_amp_node = -2;
                default: red_amp_node = -11;
            endcase
        end
    endfunction

    function integer red_y_node;
        input integer index;
        begin
            case (index)
                0: red_y_node = -287;
                1: red_y_node = -123;
                2: red_y_node = 48;
                3: red_y_node = 169;
                4: red_y_node = 186;
                5: red_y_node = 82;
                6: red_y_node = -106;
                7: red_y_node = -306;
                default: red_y_node = -505;
            endcase
        end
    endfunction

    function integer blue_base_node;
        input integer index;
        begin
            case (index)
                0: blue_base_node = 21;
                1: blue_base_node = 22;
                2: blue_base_node = 27;
                3: blue_base_node = 36;
                4: blue_base_node = 39;
                5: blue_base_node = 36;
                6: blue_base_node = 26;
                7: blue_base_node = 10;
                8: blue_base_node = -6;
                9: blue_base_node = -16;
                default: blue_base_node = -23;
            endcase
        end
    endfunction

    function integer blue_amp_node;
        input integer index;
        begin
            case (index)
                0: blue_amp_node = 3;
                1: blue_amp_node = 14;
                2: blue_amp_node = 26;
                3: blue_amp_node = 38;
                4: blue_amp_node = 43;
                5: blue_amp_node = 41;
                6: blue_amp_node = 36;
                7: blue_amp_node = 25;
                8: blue_amp_node = 12;
                9: blue_amp_node = 2;
                default: blue_amp_node = -6;
            endcase
        end
    endfunction

    function integer blue_y_node;
        input integer index;
        begin
            case (index)
                0: blue_y_node = -143;
                1: blue_y_node = -35;
                2: blue_y_node = 75;
                3: blue_y_node = 141;
                4: blue_y_node = 128;
                5: blue_y_node = 22;
                6: blue_y_node = -138;
                7: blue_y_node = -295;
                default: blue_y_node = -452;
            endcase
        end
    endfunction

    function integer interpolate_64;
        input integer value0;
        input integer value1;
        input integer fraction;
        begin
            interpolate_64 = value0 +
                             (((value1 - value0) * fraction) >>> 6);
        end
    endfunction

    function integer clamp_gain_q8;
        input integer gain_q8;
        begin
            if (gain_q8 < GAIN_MIN_Q8)
                clamp_gain_q8 = GAIN_MIN_Q8;
            else if (gain_q8 > GAIN_MAX_Q8)
                clamp_gain_q8 = GAIN_MAX_Q8;
            else
                clamp_gain_q8 = gain_q8;
        end
    endfunction

    function [7:0] scale_and_saturate;
        input [7:0] value;
        input integer gain_q8;
        integer scaled;
        begin
            scaled = ((value * gain_q8) + 128) >>> 8;
            if (scaled < 0)
                scale_and_saturate = 8'd0;
            else if (scaled > 255)
                scale_and_saturate = 8'd255;
            else
                scale_and_saturate = scaled[7:0];
        end
    endfunction

    // While unlocked, discard non-SOF words during active video. If SOF is
    // already visible, hold it until the next HDMI frame boundary.
    assign s_axis_tready = !rst && startup_armed && active &&
                           (stream_locked || !s_axis_tvalid ||
                            !s_axis_tuser || timing_frame_start);

    assign red = red_pipe;
    assign green = green_pipe;
    assign blue = blue_pipe;
    assign de = de_pipe;
    assign hsync = hsync_pipe;
    assign vsync = vsync_pipe;
    assign frame_start = frame_start_pipe;

    video_timing #(
        .H_ACTIVE(H_ACTIVE),
        .H_TOTAL(H_TOTAL),
        .V_TOTAL(V_TOTAL)
    ) timing_i (
        .pix_clk(pix_clk),
        .rst(rst),
        .x(x),
        .y(y),
        .active(active),
        .hsync(timing_hsync),
        .vsync(timing_vsync),
        .frame_start(timing_frame_start)
    );

    // Hold AXI back for the first complete timing frame after reset. This
    // gives the upstream clock converter a deterministic reset-release window
    // and prevents an SOF handshake on the same edge that resets are removed.
    always @(posedge pix_clk) begin
        if (rst)
            startup_armed <= 1'b0;
        else if (timing_frame_start)
            startup_armed <= 1'b1;
    end

    // Stage 1 performs the table lookups and horizontal/vertical interpolation.
    always @(posedge pix_clk) begin
        if (rst) begin
            red_s1         <= 8'd0;
            green_s1       <= 8'd0;
            blue_s1        <= 8'd0;
            usable_s1      <= 1'b0;
            de_s1          <= 1'b0;
            hsync_s1       <= 1'b1;
            vsync_s1       <= 1'b1;
            frame_start_s1 <= 1'b0;
            red_base_s1    <= 0;
            red_amp_s1     <= 0;
            red_y_s1       <= 0;
            blue_base_s1   <= 0;
            blue_amp_s1    <= 0;
            blue_y_s1      <= 0;
        end else begin
            red_s1         <= raw_red;
            green_s1       <= raw_green;
            blue_s1        <= raw_blue;
            usable_s1      <= pixel_usable;
            de_s1          <= active;
            hsync_s1       <= timing_hsync;
            vsync_s1       <= timing_vsync;
            frame_start_s1 <= timing_frame_start;

            if (pixel_usable) begin
                red_base_s1 <= interpolate_64(
                    red_base_node(x >> 6), red_base_node((x >> 6) + 1), x[5:0]);
                red_amp_s1 <= interpolate_64(
                    red_amp_node(x >> 6), red_amp_node((x >> 6) + 1), x[5:0]);
                red_y_s1 <= interpolate_64(
                    red_y_node(y >> 6), red_y_node((y >> 6) + 1), y[5:0]);
                blue_base_s1 <= interpolate_64(
                    blue_base_node(x >> 6), blue_base_node((x >> 6) + 1), x[5:0]);
                blue_amp_s1 <= interpolate_64(
                    blue_amp_node(x >> 6), blue_amp_node((x >> 6) + 1), x[5:0]);
                blue_y_s1 <= interpolate_64(
                    blue_y_node(y >> 6), blue_y_node((y >> 6) + 1), y[5:0]);
            end else begin
                red_base_s1  <= 0;
                red_amp_s1   <= 0;
                red_y_s1     <= 0;
                blue_base_s1 <= 0;
                blue_amp_s1  <= 0;
                blue_y_s1    <= 0;
            end
        end
    end

    // Stage 2 computes and clamps the Q8 gains. The clamp does not change the
    // fitted coefficients, but prevents future calibration errors from
    // producing negative gain or excessive highlight clipping.
    always @(posedge pix_clk) begin
        if (rst) begin
            red_s2         <= 8'd0;
            green_s2       <= 8'd0;
            blue_s2        <= 8'd0;
            usable_s2      <= 1'b0;
            de_s2          <= 1'b0;
            hsync_s2       <= 1'b1;
            vsync_s2       <= 1'b1;
            frame_start_s2 <= 1'b0;
            red_gain_s2    <= 256;
            blue_gain_s2   <= 256;
        end else begin
            red_s2         <= red_s1;
            green_s2       <= green_s1;
            blue_s2        <= blue_s1;
            usable_s2      <= usable_s1;
            de_s2          <= de_s1;
            hsync_s2       <= hsync_s1;
            vsync_s2       <= vsync_s1;
            frame_start_s2 <= frame_start_s1;

            if (usable_s1) begin
                red_gain_s2 <= clamp_gain_q8(
                    256 + red_base_s1 + ((red_amp_s1 * red_y_s1) >>> 8));
                blue_gain_s2 <= clamp_gain_q8(
                    256 + blue_base_s1 + ((blue_amp_s1 * blue_y_s1) >>> 8));
            end else begin
                red_gain_s2  <= 256;
                blue_gain_s2 <= 256;
            end
        end
    end

    // Stage 3 applies the gains and keeps all HDMI timing signals aligned with
    // the corrected RGB data.
    always @(posedge pix_clk) begin
        if (rst) begin
            red_pipe         <= 8'd0;
            green_pipe       <= 8'd0;
            blue_pipe        <= 8'd0;
            de_pipe          <= 1'b0;
            hsync_pipe       <= 1'b1;
            vsync_pipe       <= 1'b1;
            frame_start_pipe <= 1'b0;
        end else begin
            if (usable_s2) begin
                if (COLOR_CORRECTION_ENABLE != 0) begin
                    red_pipe  <= scale_and_saturate(red_s2, red_gain_s2);
                    blue_pipe <= scale_and_saturate(blue_s2, blue_gain_s2);
                end else begin
                    red_pipe  <= red_s2;
                    blue_pipe <= blue_s2;
                end
                green_pipe <= green_s2;
            end else begin
                red_pipe   <= 8'd0;
                green_pipe <= 8'd0;
                blue_pipe  <= 8'd0;
            end
            de_pipe          <= de_s2;
            hsync_pipe       <= hsync_s2;
            vsync_pipe       <= vsync_s2;
            frame_start_pipe <= frame_start_s2;
        end
    end

    always @(posedge pix_clk) begin
        if (rst) begin
            stream_locked         <= 1'b0;
            video_running         <= 1'b0;
            underflow_error       <= 1'b0;
            sync_error            <= 1'b0;
            displayed_frame_count <= 32'd0;
            underflow_count       <= 32'd0;
        end else begin
            if (active && stream_locked && !s_axis_tvalid) begin
                stream_locked   <= 1'b0;
                underflow_error <= 1'b1;
                underflow_count <= underflow_count + 1'b1;
            end

            if (transfer) begin
                if (!stream_locked) begin
                    if (s_axis_tuser && timing_frame_start) begin
                        stream_locked <= 1'b1;
                        video_running <= 1'b1;
                    end
                end else begin
                    if (s_axis_tuser != timing_frame_start) begin
                        stream_locked <= 1'b0;
                        sync_error    <= 1'b1;
                    end
                    if (s_axis_tlast != (x == H_ACTIVE-1)) begin
                        stream_locked <= 1'b0;
                        sync_error    <= 1'b1;
                    end
                    if (timing_frame_start && s_axis_tuser)
                        displayed_frame_count <= displayed_frame_count + 1'b1;
                end
            end
        end
    end
endmodule
