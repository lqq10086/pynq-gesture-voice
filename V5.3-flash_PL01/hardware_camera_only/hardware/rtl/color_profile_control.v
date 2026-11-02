`timescale 1ns/1ps

// Synchronizes and debounces the two J11 expansion-board switches, then maps
// their stable state to the requested post-lens FPGA color-pipeline profile.
module color_profile_control #(
    parameter integer DEBOUNCE_CYCLES = 1000000,   // 20 ms at 50 MHz
    parameter integer AUTO_CYCLES     = 250000000  // 5 s at 50 MHz
) (
    input  wire       clk,
    input  wire       rst,
    input  wire       sw0_async,
    input  wire       sw1_async,
    output reg  [1:0] stable_mode,
    output reg        mode_changed,
    output reg        color_profile_request,
    output wire       automatic_mode
);
    localparam integer DEBOUNCE_COUNT_WIDTH =
        (DEBOUNCE_CYCLES <= 1) ? 1 : $clog2(DEBOUNCE_CYCLES);
    localparam integer AUTO_COUNT_WIDTH =
        (AUTO_CYCLES <= 1) ? 1 : $clog2(AUTO_CYCLES);

    (* ASYNC_REG = "TRUE" *) reg [1:0] sw0_sync;
    (* ASYNC_REG = "TRUE" *) reg [1:0] sw1_sync;
    reg [DEBOUNCE_COUNT_WIDTH-1:0] sw0_count;
    reg [DEBOUNCE_COUNT_WIDTH-1:0] sw1_count;
    reg [AUTO_COUNT_WIDTH-1:0] auto_count;
    reg sw0_stable;
    reg sw1_stable;
    reg [1:0] last_applied_mode;

    wire [1:0] debounced_mode = {sw1_stable, sw0_stable};
    assign automatic_mode = stable_mode[1];

    initial begin
        if (DEBOUNCE_CYCLES < 1)
            $error("DEBOUNCE_CYCLES must be at least 1");
        if (AUTO_CYCLES < 1)
            $error("AUTO_CYCLES must be at least 1");
    end

    always @(posedge clk) begin
        if (rst) begin
            sw0_sync             <= 2'b00;
            sw1_sync             <= 2'b00;
            sw0_count            <= 0;
            sw1_count            <= 0;
            sw0_stable           <= 1'b0;
            sw1_stable           <= 1'b0;
            stable_mode          <= 2'b00;
            last_applied_mode    <= 2'b00;
            mode_changed         <= 1'b0;
            color_profile_request <= 1'b0;
            auto_count           <= 0;
        end else begin
            sw0_sync <= {sw0_sync[0], sw0_async};
            sw1_sync <= {sw1_sync[0], sw1_async};
            mode_changed <= 1'b0;

            if (sw0_sync[1] == sw0_stable) begin
                sw0_count <= 0;
            end else if (sw0_count == DEBOUNCE_CYCLES-1) begin
                sw0_stable <= sw0_sync[1];
                sw0_count  <= 0;
            end else begin
                sw0_count <= sw0_count + 1'b1;
            end

            if (sw1_sync[1] == sw1_stable) begin
                sw1_count <= 0;
            end else if (sw1_count == DEBOUNCE_CYCLES-1) begin
                sw1_stable <= sw1_sync[1];
                sw1_count  <= 0;
            end else begin
                sw1_count <= sw1_count + 1'b1;
            end

            stable_mode <= debounced_mode;
            if (debounced_mode != last_applied_mode) begin
                last_applied_mode <= debounced_mode;
                mode_changed <= 1'b1;
                auto_count <= 0;
                case (debounced_mode)
                    2'b00: color_profile_request <= 1'b0;
                    2'b01: color_profile_request <= 1'b1;
                    2'b10: color_profile_request <= 1'b0;
                    2'b11: color_profile_request <= 1'b1;
                endcase
            end else if (debounced_mode[1]) begin
                if (auto_count == AUTO_CYCLES-1) begin
                    auto_count <= 0;
                    color_profile_request <= ~color_profile_request;
                end else begin
                    auto_count <= auto_count + 1'b1;
                end
            end else begin
                auto_count <= 0;
            end
        end
    end
endmodule
