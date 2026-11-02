`timescale 1ns/1ps

module video_timing #(
    parameter integer H_ACTIVE = 1280,
    parameter integer H_FP     = 110,
    parameter integer H_SYNC   = 40,
    parameter integer H_BP     = 220,
    parameter integer H_TOTAL  = 1650,
    parameter integer V_ACTIVE = 720,
    parameter integer V_FP     = 5,
    parameter integer V_SYNC   = 5,
    parameter integer V_BP     = 20,
    parameter integer V_TOTAL  = 750
) (
    input  wire       pix_clk,
    input  wire       rst,
    output reg [$clog2(H_TOTAL)-1:0] x,
    output reg [$clog2(V_TOTAL)-1:0] y,
    output wire       active,
    output wire       hsync,
    output wire       vsync,
    output wire       frame_start
);
    assign active      = (x < H_ACTIVE) && (y < V_ACTIVE);
    assign hsync       = ((x >= H_ACTIVE + H_FP) && (x < H_ACTIVE + H_FP + H_SYNC));
    assign vsync       = ((y >= V_ACTIVE + V_FP) && (y < V_ACTIVE + V_FP + V_SYNC));
    assign frame_start = (x == 0) && (y == 0);

    always @(posedge pix_clk) begin
        if (rst) begin
            x <= {$clog2(H_TOTAL){1'b0}};
            y <= {$clog2(V_TOTAL){1'b0}};
        end else if (x == H_TOTAL-1) begin
            x <= {$clog2(H_TOTAL){1'b0}};
            if (y == V_TOTAL-1)
                y <= {$clog2(V_TOTAL){1'b0}};
            else
                y <= y + 1'b1;
        end else begin
            x <= x + 1'b1;
        end
    end
endmodule
