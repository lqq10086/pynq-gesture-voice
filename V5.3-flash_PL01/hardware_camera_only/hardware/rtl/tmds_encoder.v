`timescale 1ns/1ps

module tmds_encoder (
    input  wire       clk,
    input  wire       rst,
    input  wire [7:0] video,
    input  wire       de,
    input  wire       c0,
    input  wire       c1,
    output reg  [9:0] tmds
);
    integer i;
    reg [3:0] ones_d;
    reg [3:0] ones_qm;
    reg [8:0] q_m;
    reg       use_xnor;
    reg signed [5:0] disparity;

    always @* begin
        ones_d = 0;
        for (i = 0; i < 8; i = i + 1)
            ones_d = ones_d + video[i];

        use_xnor = (ones_d > 4) || ((ones_d == 4) && (video[0] == 1'b0));
        q_m[0] = video[0];
        for (i = 1; i < 8; i = i + 1) begin
            if (use_xnor)
                q_m[i] = ~(q_m[i-1] ^ video[i]);
            else
                q_m[i] = q_m[i-1] ^ video[i];
        end
        q_m[8] = ~use_xnor;

        ones_qm = 0;
        for (i = 0; i < 8; i = i + 1)
            ones_qm = ones_qm + q_m[i];
    end

    always @(posedge clk) begin
        if (rst) begin
            tmds      <= 10'b1101010100;
            disparity <= 0;
        end else if (!de) begin
            disparity <= 0;
            case ({c1,c0})
                2'b00: tmds <= 10'b1101010100;
                2'b01: tmds <= 10'b0010101011;
                2'b10: tmds <= 10'b0101010100;
                2'b11: tmds <= 10'b1010101011;
            endcase
        end else if ((disparity == 0) || (ones_qm == 4)) begin
            tmds[9]   <= ~q_m[8];
            tmds[8]   <= q_m[8];
            tmds[7:0] <= q_m[8] ? q_m[7:0] : ~q_m[7:0];
            if (q_m[8])
                disparity <= disparity + $signed({1'b0,ones_qm}) - $signed(5'd8 - {1'b0,ones_qm});
            else
                disparity <= disparity + $signed(5'd8 - {1'b0,ones_qm}) - $signed({1'b0,ones_qm});
        end else if (((disparity > 0) && (ones_qm > 4)) ||
                     ((disparity < 0) && (ones_qm < 4))) begin
            tmds <= {1'b1, q_m[8], ~q_m[7:0]};
            disparity <= disparity + (q_m[8] ? 2 : 0) +
                         $signed(5'd8 - {1'b0,ones_qm}) - $signed({1'b0,ones_qm});
        end else begin
            tmds <= {1'b0, q_m[8], q_m[7:0]};
            disparity <= disparity - (q_m[8] ? 0 : 2) +
                         $signed({1'b0,ones_qm}) - $signed(5'd8 - {1'b0,ones_qm});
        end
    end
endmodule
