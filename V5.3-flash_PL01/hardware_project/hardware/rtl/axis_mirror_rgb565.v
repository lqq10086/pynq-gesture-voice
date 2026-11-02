`timescale 1ns/1ps

// Two complete lines, not a frame buffer. Each line is accepted left-to-right
// and read right-to-left. WIDTH must be positive. Reset is synchronous/high.
// Malformed TLAST/SOF sets sync_error until reset; it never changes the fixed
// WIDTH-pixel grouping or silently realigns the stream.
module axis_mirror_rgb565 #(parameter integer WIDTH = 1280) (
    input wire clk, input wire rst,
    input wire [15:0] s_axis_tdata,
    input wire s_axis_tvalid, output wire s_axis_tready,
    input wire s_axis_tuser, input wire s_axis_tlast,
    output wire [15:0] m_axis_tdata,
    output reg m_axis_tvalid, input wire m_axis_tready,
    output reg m_axis_tuser, output reg m_axis_tlast,
    output reg sync_error
);
    localparam integer ADDR_BITS = (WIDTH > 1) ? $clog2(WIDTH) : 1;
    localparam [ADDR_BITS-1:0] LAST = WIDTH-1;

    (* ram_style = "block" *) reg [15:0] line0 [0:WIDTH-1];
    (* ram_style = "block" *) reg [15:0] line1 [0:WIDTH-1];
    reg [15:0] read_data0, read_data1;
    reg output_bank;
    reg write_bank, read_bank;
    reg [ADDR_BITS-1:0] write_x, read_x;
    reg [1:0] bank_full, bank_sof;

    assign s_axis_tready = !rst && !bank_full[write_bank];
    assign m_axis_tdata = output_bank ? read_data1 : read_data0;
    wire input_fire = s_axis_tvalid && s_axis_tready;
    wire output_slot = !m_axis_tvalid || m_axis_tready;
    wire load_output = !rst && output_slot && bank_full[read_bank];
    wire complete_line = input_fire && write_x == LAST;
    wire release_line = load_output && read_x == 0;
    wire [1:0] complete_mask = complete_line ?
                              (write_bank ? 2'b10 : 2'b01) : 2'b00;
    wire [1:0] release_mask = release_line ?
                             (read_bank ? 2'b10 : 2'b01) : 2'b00;

    // Independent synchronous read ports infer two BRAM banks. Their read
    // registers plus output_bank form one elastic output slot; no RAM reset.
    always @(posedge clk) begin
        if (input_fire && !write_bank) line0[write_x] <= s_axis_tdata;
        if (input_fire &&  write_bank) line1[write_x] <= s_axis_tdata;
        if (load_output && !read_bank) read_data0 <= line0[read_x];
        if (load_output &&  read_bank) read_data1 <= line1[read_x];
    end

    always @(posedge clk) begin
        if (rst) begin
            write_bank <= 0;
            read_bank <= 0;
            write_x <= 0;
            read_x <= LAST;
            bank_full <= 0;
            bank_sof <= 0;
            output_bank <= 0;
            m_axis_tvalid <= 0;
            m_axis_tuser <= 0;
            m_axis_tlast <= 0;
            sync_error <= 0;
        end else begin
            // Completing one bank and releasing the other on the same cycle
            // must preserve both updates. A bank remains full until its last
            // read has entered the elastic slot, even under backpressure.
            bank_full <= (bank_full | complete_mask) & ~release_mask;
            if (input_fire) begin
                if ((s_axis_tlast != (write_x == LAST)) ||
                    (s_axis_tuser && write_x != 0)) sync_error <= 1;
                if (write_x == 0) bank_sof[write_bank] <= s_axis_tuser;
                if (complete_line) begin
                    write_x <= 0;
                    write_bank <= !write_bank;
                end else write_x <= write_x + 1'b1;
            end
            if (output_slot) m_axis_tvalid <= bank_full[read_bank];
            if (load_output) begin
                output_bank <= read_bank;
                m_axis_tuser <= bank_sof[read_bank] && read_x == LAST;
                m_axis_tlast <= read_x == 0;
                if (release_line) begin
                    read_x <= LAST;
                    read_bank <= !read_bank;
                end else read_x <= read_x - 1'b1;
            end
        end
    end
endmodule
