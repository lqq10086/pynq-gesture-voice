`timescale 1ns/1ps

module sccb_write (
    input  wire        clk,
    input  wire        rst,
    input  wire        start,
    input  wire [31:0] payload,
    input  wire        read_enable,
    output reg  [7:0]  read_data,
    output reg         scl,
    inout  wire        sda,
    output reg         busy,
    output reg         done,
    output reg         ack_error
);
    localparam DIVIDER = 125; // 50 MHz / 125 = 400 kHz state tick.
    localparam IDLE=0, START_A=1, START_B=2, BIT_SETUP=3, BIT_HIGH=4,
               BIT_LOW=5, ACK_SETUP=6, ACK_HIGH=7, ACK_LOW=8,
               STOP_A=9, STOP_B=10, STOP_C=11,
               READ_SETUP=12, READ_HIGH=13, READ_LOW=14,
               NACK_SETUP=15, NACK_HIGH=16, NACK_LOW=17;

    reg [7:0] div_count;
    reg [4:0] state;
    reg read_transaction, read_phase;
    reg [7:0] device_read_address;
    reg [31:0] shift;
    reg [2:0] bit_count;
    reg [2:0] byte_count;
    reg sda_low;
    (* ASYNC_REG = "TRUE" *) reg [1:0] sda_sync;
    wire tick = (div_count == DIVIDER-1);

    assign sda = sda_low ? 1'b0 : 1'bz;

    always @(posedge clk) begin
        if (rst) begin
            sda_sync   <= 2'b11;
            div_count  <= 0;
            state      <= IDLE;
            shift      <= 0;
            bit_count  <= 7;
            byte_count <= 0;
            scl        <= 1'b1;
            sda_low    <= 1'b0;
            busy       <= 1'b0;
            done       <= 1'b0;
            ack_error  <= 1'b0;
            read_data <= 0;
            read_transaction <= 0;
            read_phase <= 0;
            device_read_address <= 0;
        end else begin
            sda_sync <= {sda_sync[0], sda};
            done <= 1'b0;
            if (tick)
                div_count <= 0;
            else
                div_count <= div_count + 1'b1;

            if (state == IDLE) begin
                scl     <= 1'b1;
                sda_low <= 1'b0;
                busy    <= 1'b0;
                if (start) begin
                    shift      <= payload;
                    read_transaction <= read_enable;
                    read_phase <= 0;
                    read_data <= 0;
                    device_read_address <= payload[31:24] | 8'h01;
                    bit_count  <= 7;
                    byte_count <= 0;
                    busy       <= 1'b1;
                    ack_error  <= 1'b0;
                    div_count  <= 0;
                    state      <= START_A;
                end
            end else if (tick) begin
                case (state)
                    START_A: begin scl <= 1'b1; sda_low <= 1'b0; state <= START_B; end
                    START_B: begin scl <= 1'b1; sda_low <= 1'b1; state <= BIT_SETUP; end
                    BIT_SETUP: begin
                        scl     <= 1'b0;
                        sda_low <= ~shift[31];
                        state   <= BIT_HIGH;
                    end
                    BIT_HIGH: begin scl <= 1'b1; state <= BIT_LOW; end
                    BIT_LOW: begin
                        scl   <= 1'b0;
                        shift <= {shift[30:0],1'b0};
                        if (bit_count == 0) begin
                            bit_count <= 7;
                            state <= ACK_SETUP;
                        end else begin
                            bit_count <= bit_count - 1'b1;
                            state <= BIT_SETUP;
                        end
                    end
                    ACK_SETUP: begin scl <= 1'b0; sda_low <= 1'b0; state <= ACK_HIGH; end
                    ACK_HIGH: begin
                        scl <= 1'b1;
                        state <= ACK_LOW;
                    end
                    ACK_LOW: begin
                        scl <= 1'b0;
                        // Sample at the end of SCL high, after the input synchronizer.
                        if (sda_sync[1] !== 1'b0)
                            ack_error <= 1'b1;
                        if (read_phase) begin
                            bit_count <= 7;
                            state <= READ_SETUP;
                        end else if (byte_count == 3 || (read_transaction && byte_count == 2))
                            state <= STOP_A;
                        else begin
                            byte_count <= byte_count + 1'b1;
                            state <= BIT_SETUP;
                        end
                    end
                    STOP_A: begin scl <= 1'b0; sda_low <= 1'b1; state <= STOP_B; end
                    STOP_B: begin scl <= 1'b1; sda_low <= 1'b1; state <= STOP_C; end
                    STOP_C: begin
                        scl     <= 1'b1;
                        sda_low <= 1'b0;
                        // The local Waveshare SCCB read uses STOP then START.
                        if (read_transaction && !read_phase) begin
                            read_phase <= 1;
                            shift <= {device_read_address,24'd0};
                            bit_count <= 7;
                            byte_count <= 0;
                            state <= START_A;
                        end else begin
                            busy    <= 1'b0;
                            done    <= 1'b1;
                            state   <= IDLE;
                        end
                    end
                    READ_SETUP: begin scl <= 0; sda_low <= 0; state <= READ_HIGH; end
                    READ_HIGH: begin scl <= 1; state <= READ_LOW; end
                    READ_LOW: begin
                        scl <= 0;
                        read_data <= {read_data[6:0],sda_sync[1]};
                        if (bit_count == 0) state <= NACK_SETUP;
                        else begin bit_count <= bit_count-1'b1; state <= READ_SETUP; end
                    end
                    NACK_SETUP: begin scl <= 0; sda_low <= 0; state <= NACK_HIGH; end
                    NACK_HIGH: begin scl <= 1; state <= NACK_LOW; end
                    NACK_LOW: begin scl <= 0; state <= STOP_A; end
                    default: state <= IDLE;
                endcase
            end
        end
    end
endmodule
