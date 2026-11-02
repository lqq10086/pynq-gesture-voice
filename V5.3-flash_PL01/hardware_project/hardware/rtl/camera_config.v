`timescale 1ns/1ps
`include "ov5640_color_delta_count.vh"
`include "ov5640_isp_baseline_count.vh"

module camera_config #(
    parameter integer POWER_DOWN_CYCLES        = 500000,
    parameter integer RESET_ASSERT_END_CYCLES  = 750000,
    parameter integer RESET_RELEASE_END_CYCLES = 1750000,
    parameter integer POST_RESET_DELAY_CYCLES  = 250000,
    parameter integer RETRY_DELAY_CYCLES       = 50000,
    parameter integer ROM_ENTRY_COUNT          = 261,
    parameter         INIT_MEM_FILE            = "ov5640_vga_rgb565.mem",
    parameter integer COLOR_DELTA_COUNT         = `OV5640_COLOR_DELTA_COUNT,
    parameter         COLOR_DELTA_MEM_FILE      = "ov5640_color_delta.mem",
    parameter integer RUNTIME_MAX_RETRIES       = 3,
    parameter integer SETTLE_FRAME_COUNT        = 3,
    parameter integer AF_ENABLE                = 0,
    parameter integer ISP_PROFILE_ENABLE       = 0,
    parameter integer ISP_DELTA_COUNT          = `OV5640_ISP_BASELINE_COUNT,
    parameter ISP_MEM_FILE                     = "ov5640_isp_baseline.mem"
) (
    input  wire clk50,
    input  wire rst,
    input  wire frame_boundary,
    input  wire color_profile_request,
    input  wire isp_profile_select,
    output reg  cam_pwdn,
    output reg  cam_reset_n,
    output wire cam_scl,
    inout  wire cam_sda,
    output reg  config_done,
    output reg  config_error,
    output reg  color_profile_active,
    output reg  color_profile_busy,
    output reg  color_profile_error,
    output reg  capture_freeze,
    output wire [15:0] af_status,
    output wire af_error
);
    localparam integer ROM_LAST_INDEX = ROM_ENTRY_COUNT - 1;
    localparam integer COLOR_DELTA_STORAGE =
        (COLOR_DELTA_COUNT > 0) ? COLOR_DELTA_COUNT : 1;

    localparam [2:0] RUNTIME_IDLE          = 3'd0;
    localparam [2:0] RUNTIME_WAIT_FRAME    = 3'd1;
    localparam [2:0] RUNTIME_WRITE_TARGET  = 3'd2;
    localparam [2:0] RUNTIME_ROLLBACK      = 3'd3;
    localparam [2:0] RUNTIME_SETTLE        = 3'd4;

    reg [23:0] rom [0:ROM_ENTRY_COUNT-1];
    reg [23:0] isp_rom [0:ISP_DELTA_COUNT-1];
    reg isp_loading;
    reg [8:0] isp_index;
    // Each word is {register[15:0], before_value[7:0], after_value[7:0]}.
    reg [31:0] color_delta_rom [0:COLOR_DELTA_STORAGE-1];
    reg [8:0]  index;
    reg [8:0]  runtime_index;
    reg [20:0] delay_count;
    reg [18:0] post_reset_delay;
    reg [15:0] retry_delay;
    reg [7:0]  runtime_retry_count;
    reg [7:0]  settle_frame_count;
    reg [2:0]  runtime_state;
    reg        requested_profile;
    reg        previous_profile;
    reg        transition_success;
    reg        start;
    wire       busy;
    wire       done;
    wire       ack_error;
    wire af_start, af_read;
    wire [23:0] af_payload;
    wire [7:0] read_data;
    wire af_owner = (AF_ENABLE != 0) && config_done;
    generate if (AF_ENABLE != 0) begin : gen_af
        ov5640_af af_i (
            .clk(clk50), .rst(rst), .enable(config_done),
            .start(af_start), .read_enable(af_read), .payload(af_payload),
            .busy(busy), .done(done), .ack_error(ack_error),
            .read_data(read_data), .status(af_status), .failed(af_error)
        );
    end else begin : gen_no_af
        assign af_start=0;
        assign af_read=0;
        assign af_payload=0;
        assign af_status=0;
        assign af_error=0;
    end endgenerate

    wire [23:0] runtime_target_write = {
        color_delta_rom[runtime_index][31:16],
        requested_profile ? color_delta_rom[runtime_index][7:0]
                          : color_delta_rom[runtime_index][15:8]
    };
    wire [23:0] runtime_rollback_write = {
        color_delta_rom[runtime_index][31:16],
        previous_profile ? color_delta_rom[runtime_index][7:0]
                         : color_delta_rom[runtime_index][15:8]
    };
    wire [23:0] selected_write =
        !config_done ? (isp_loading ? isp_rom[isp_index] : rom[index]) :
        (runtime_state == RUNTIME_ROLLBACK) ? runtime_rollback_write :
        runtime_target_write;

    initial begin
        if (ROM_ENTRY_COUNT < 2 || ROM_ENTRY_COUNT > 512)
            $error("OV5640 ROM_ENTRY_COUNT must be between 2 and 512");
        if (COLOR_DELTA_COUNT < 0 || COLOR_DELTA_COUNT > 512)
            $error("OV5640 COLOR_DELTA_COUNT must be between 0 and 512");
        if (RUNTIME_MAX_RETRIES < 0 || RUNTIME_MAX_RETRIES > 255)
            $error("RUNTIME_MAX_RETRIES must be between 0 and 255");
        if (SETTLE_FRAME_COUNT < 1 || SETTLE_FRAME_COUNT > 255)
            $error("SETTLE_FRAME_COUNT must be between 1 and 255");
        $readmemh(INIT_MEM_FILE, rom);
        if (ISP_PROFILE_ENABLE) $readmemh(ISP_MEM_FILE, isp_rom);
        if (COLOR_DELTA_COUNT > 0)
            $readmemh(COLOR_DELTA_MEM_FILE, color_delta_rom);
        else
            color_delta_rom[0] = 32'd0;
    end

    sccb_write writer_i (
        .clk(clk50),
        .rst(rst),
        .start(af_owner ? af_start : start),
        .payload({8'h78, af_owner ? af_payload : selected_write}),
        .read_enable(af_owner && af_read),
        .read_data(read_data),
        .scl(cam_scl),
        .sda(cam_sda),
        .busy(busy),
        .done(done),
        .ack_error(ack_error)
    );

    always @(posedge clk50) begin
        if (rst) begin
            cam_pwdn              <= 1'b1;
            cam_reset_n           <= 1'b0;
            config_done           <= 1'b0;
            config_error          <= 1'b0;
            color_profile_active  <= 1'b0;
            color_profile_busy    <= 1'b0;
            color_profile_error   <= 1'b0;
            capture_freeze        <= 1'b0;
            delay_count           <= 0;
            post_reset_delay      <= 0;
            retry_delay           <= 0;
            runtime_retry_count   <= 0;
            settle_frame_count    <= 0;
            runtime_state         <= RUNTIME_IDLE;
            requested_profile     <= 1'b0;
            previous_profile      <= 1'b0;
            transition_success    <= 1'b0;
            index                 <= 0;
            isp_loading           <= 0;
            isp_index             <= 0;
            runtime_index         <= 0;
            start                 <= 1'b0;
        end else begin
            start <= 1'b0;
            if (delay_count < POWER_DOWN_CYCLES) begin
                delay_count <= delay_count + 1'b1;
                cam_pwdn    <= 1'b1;
                cam_reset_n <= 1'b0;
            end else if (delay_count < RESET_ASSERT_END_CYCLES) begin
                delay_count <= delay_count + 1'b1;
                cam_pwdn    <= 1'b0;
                cam_reset_n <= 1'b0;
            end else if (delay_count < RESET_RELEASE_END_CYCLES) begin
                delay_count <= delay_count + 1'b1;
                cam_pwdn    <= 1'b0;
                cam_reset_n <= 1'b1;
            end else if (post_reset_delay != 0) begin
                post_reset_delay <= post_reset_delay - 1'b1;
            end else if (retry_delay != 0) begin
                retry_delay <= retry_delay - 1'b1;
            end else if (!config_done) begin
                if (!busy && !done)
                    start <= 1'b1;
                if (done) begin
                    if (ack_error) begin
                        config_error <= 1'b1;
                        retry_delay  <= RETRY_DELAY_CYCLES;
                    end else if (isp_loading) begin
                        if (isp_index == ISP_DELTA_COUNT-1)
                            config_done <= 1'b1;
                        else isp_index <= isp_index + 1'b1;
                    end else if (index == ROM_LAST_INDEX) begin
                        if (ISP_PROFILE_ENABLE && isp_profile_select) begin
                            isp_loading <= 1'b1;
                            isp_index <= 0;
                        end else config_done <= 1'b1;
                    end else if (index == 9'd1) begin
                        index            <= index + 1'b1;
                        post_reset_delay <= POST_RESET_DELAY_CYCLES;
                    end else begin
                        index <= index + 1'b1;
                    end
                end
            end else if (!af_owner) begin
                case (runtime_state)
                    RUNTIME_IDLE: begin
                        color_profile_busy <= 1'b0;
                        capture_freeze     <= 1'b0;
                        runtime_retry_count <= 0;
                        settle_frame_count <= 0;
                        if (color_profile_request != color_profile_active) begin
                            requested_profile  <= color_profile_request;
                            previous_profile   <= color_profile_active;
                            color_profile_busy <= 1'b1;
                            capture_freeze     <= 1'b1;
                            runtime_index      <= 0;
                            transition_success <= 1'b0;
                            runtime_state      <= RUNTIME_WAIT_FRAME;
                        end
                    end

                    RUNTIME_WAIT_FRAME: begin
                        // SCCB writes begin only after the next complete sensor
                        // frame boundary. capture_freeze was asserted earlier,
                        // so the DVP-to-AXI path will not arm a mixed frame.
                        if (frame_boundary) begin
                            runtime_index       <= 0;
                            runtime_retry_count <= 0;
                            if (COLOR_DELTA_COUNT == 0) begin
                                transition_success <= 1'b1;
                                settle_frame_count <= 0;
                                runtime_state <= RUNTIME_SETTLE;
                            end else begin
                                runtime_state <= RUNTIME_WRITE_TARGET;
                            end
                        end
                    end

                    RUNTIME_WRITE_TARGET: begin
                        if (!busy && !done)
                            start <= 1'b1;
                        if (done) begin
                            if (ack_error) begin
                                if (runtime_retry_count < RUNTIME_MAX_RETRIES) begin
                                    runtime_retry_count <= runtime_retry_count + 1'b1;
                                    retry_delay <= RETRY_DELAY_CYCLES;
                                end else begin
                                    color_profile_error <= 1'b1;
                                    runtime_retry_count <= 0;
                                    runtime_index <= 0;
                                    transition_success <= 1'b0;
                                    runtime_state <= RUNTIME_ROLLBACK;
                                end
                            end else begin
                                runtime_retry_count <= 0;
                                if (runtime_index == COLOR_DELTA_COUNT-1) begin
                                    transition_success <= 1'b1;
                                    settle_frame_count <= 0;
                                    runtime_state <= RUNTIME_SETTLE;
                                end else begin
                                    runtime_index <= runtime_index + 1'b1;
                                end
                            end
                        end
                    end

                    RUNTIME_ROLLBACK: begin
                        // Reapply the complete old-value side of the delta.
                        // This restores every color register that could have
                        // been changed before a later SCCB write failed.
                        if (!busy && !done)
                            start <= 1'b1;
                        if (done) begin
                            if (ack_error) begin
                                if (runtime_retry_count < RUNTIME_MAX_RETRIES) begin
                                    runtime_retry_count <= runtime_retry_count + 1'b1;
                                    retry_delay <= RETRY_DELAY_CYCLES;
                                end else begin
                                    color_profile_error <= 1'b1;
                                    settle_frame_count <= 0;
                                    runtime_state <= RUNTIME_SETTLE;
                                end
                            end else begin
                                runtime_retry_count <= 0;
                                if (runtime_index == COLOR_DELTA_COUNT-1) begin
                                    settle_frame_count <= 0;
                                    runtime_state <= RUNTIME_SETTLE;
                                end else begin
                                    runtime_index <= runtime_index + 1'b1;
                                end
                            end
                        end
                    end

                    RUNTIME_SETTLE: begin
                        // HDMI continues reading the last complete DDR frame.
                        // Count raw sensor frames before releasing capture.
                        if (frame_boundary) begin
                            if (settle_frame_count == SETTLE_FRAME_COUNT-1) begin
                                if (transition_success)
                                    color_profile_active <= requested_profile;
                                color_profile_busy <= 1'b0;
                                capture_freeze <= 1'b0;
                                runtime_state <= RUNTIME_IDLE;
                                settle_frame_count <= 0;
                            end else begin
                                settle_frame_count <= settle_frame_count + 1'b1;
                            end
                        end
                    end

                    default: begin
                        color_profile_busy <= 1'b0;
                        capture_freeze <= 1'b0;
                        runtime_state <= RUNTIME_IDLE;
                    end
                endcase
            end
        end
    end
endmodule
