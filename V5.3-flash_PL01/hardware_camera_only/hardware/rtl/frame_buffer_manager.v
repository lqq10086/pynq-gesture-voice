`timescale 1ns/1ps

// Synthesizable reference model for the software-owned VDMA parking policy.
// The deployed bare-metal program implements the same transitions in ISRs.
module frame_buffer_manager (
    input  wire        clk,
    input  wire        rst,
    input  wire        capture_frame_done,
    input  wire        display_frame_done,
    output reg  [1:0]  write_index,
    output reg  [1:0]  ready_index,
    output reg         ready_valid,
    output reg  [1:0]  display_index,
    output reg  [1:0]  state0,
    output reg  [1:0]  state1,
    output reg  [1:0]  state2,
    output reg  [31:0] s2mm_frame_count,
    output reg  [31:0] mm2s_frame_count,
    output reg  [31:0] dropped_frame_count,
    output reg  [31:0] repeated_frame_count,
    output reg  [31:0] buffer_collision_count
);
    localparam [1:0] FREE       = 2'd0;
    localparam [1:0] WRITING    = 2'd1;
    localparam [1:0] READY      = 2'd2;
    localparam [1:0] DISPLAYING = 2'd3;

    reg [1:0] write_index_n;
    reg [1:0] ready_index_n;
    reg       ready_valid_n;
    reg [1:0] display_index_n;
    reg [1:0] state0_n;
    reg [1:0] state1_n;
    reg [1:0] state2_n;
    reg [31:0] s2mm_frame_count_n;
    reg [31:0] mm2s_frame_count_n;
    reg [31:0] dropped_frame_count_n;
    reg [31:0] repeated_frame_count_n;
    reg [31:0] buffer_collision_count_n;
    reg [1:0] next_write;
    reg       next_write_found;

    always @* begin
        write_index_n = write_index;
        ready_index_n = ready_index;
        ready_valid_n = ready_valid;
        display_index_n = display_index;
        state0_n = state0;
        state1_n = state1;
        state2_n = state2;
        s2mm_frame_count_n = s2mm_frame_count;
        mm2s_frame_count_n = mm2s_frame_count;
        dropped_frame_count_n = dropped_frame_count;
        repeated_frame_count_n = repeated_frame_count;
        buffer_collision_count_n = buffer_collision_count;
        next_write = write_index;
        next_write_found = 1'b0;

        // The display boundary is processed first. A READY frame can only
        // become DISPLAYING here, never in the capture-complete path.
        if (display_frame_done) begin
            mm2s_frame_count_n = mm2s_frame_count_n + 1'b1;
            if (ready_valid_n) begin
                case (display_index_n)
                    2'd0: state0_n = FREE;
                    2'd1: state1_n = FREE;
                    2'd2: state2_n = FREE;
                    default: buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                endcase
                case (ready_index_n)
                    2'd0: state0_n = DISPLAYING;
                    2'd1: state1_n = DISPLAYING;
                    2'd2: state2_n = DISPLAYING;
                    default: buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                endcase
                display_index_n = ready_index_n;
                ready_valid_n = 1'b0;
            end else begin
                repeated_frame_count_n = repeated_frame_count_n + 1'b1;
            end
        end

        if (capture_frame_done) begin
            s2mm_frame_count_n = s2mm_frame_count_n + 1'b1;

            if ((write_index_n == display_index_n) ||
                ((write_index_n == 0) && (state0_n != WRITING)) ||
                ((write_index_n == 1) && (state1_n != WRITING)) ||
                ((write_index_n == 2) && (state2_n != WRITING))) begin
                buffer_collision_count_n = buffer_collision_count_n + 1'b1;
            end else begin
                if (ready_valid_n) begin
                    case (ready_index_n)
                        2'd0: state0_n = FREE;
                        2'd1: state1_n = FREE;
                        2'd2: state2_n = FREE;
                        default: buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                    endcase
                    dropped_frame_count_n = dropped_frame_count_n + 1'b1;
                end

                case (write_index_n)
                    2'd0: state0_n = READY;
                    2'd1: state1_n = READY;
                    2'd2: state2_n = READY;
                    default: buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                endcase
                ready_index_n = write_index_n;
                ready_valid_n = 1'b1;

                if ((state0_n == FREE) && (display_index_n != 0)) begin
                    next_write = 0;
                    next_write_found = 1'b1;
                end else if ((state1_n == FREE) && (display_index_n != 1)) begin
                    next_write = 1;
                    next_write_found = 1'b1;
                end else if ((state2_n == FREE) && (display_index_n != 2)) begin
                    next_write = 2;
                    next_write_found = 1'b1;
                end

                if (next_write_found) begin
                    write_index_n = next_write;
                    case (next_write)
                        2'd0: state0_n = WRITING;
                        2'd1: state1_n = WRITING;
                        2'd2: state2_n = WRITING;
                        default: buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                    endcase
                end else begin
                    buffer_collision_count_n = buffer_collision_count_n + 1'b1;
                end
            end
        end
    end

    always @(posedge clk) begin
        if (rst) begin
            write_index <= 2'd1;
            ready_index <= 2'd0;
            ready_valid <= 1'b0;
            display_index <= 2'd0;
            state0 <= DISPLAYING;
            state1 <= WRITING;
            state2 <= FREE;
            s2mm_frame_count <= 32'd0;
            mm2s_frame_count <= 32'd0;
            dropped_frame_count <= 32'd0;
            repeated_frame_count <= 32'd0;
            buffer_collision_count <= 32'd0;
        end else begin
            write_index <= write_index_n;
            ready_index <= ready_index_n;
            ready_valid <= ready_valid_n;
            display_index <= display_index_n;
            state0 <= state0_n;
            state1 <= state1_n;
            state2 <= state2_n;
            s2mm_frame_count <= s2mm_frame_count_n;
            mm2s_frame_count <= mm2s_frame_count_n;
            dropped_frame_count <= dropped_frame_count_n;
            repeated_frame_count <= repeated_frame_count_n;
            buffer_collision_count <= buffer_collision_count_n;
        end
    end
endmodule
