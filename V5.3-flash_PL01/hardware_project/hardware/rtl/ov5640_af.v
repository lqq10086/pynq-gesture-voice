`timescale 1ns/1ps
// Waveshare OV5640_Focus_Init + OV5640_Auto_Focus, on the sole SCCB engine.
// RUN means continuous command acknowledged, never proof of physical focus.
module ov5640_af #(
    parameter integer FW_BYTES = 4077,
    parameter FW_MEM_FILE = "af_firmware.mem",
    parameter integer POLL_CYCLES = 250000, // 5 ms at 50 MHz
    parameter integer MAX_POLLS = 1001
)(
    input wire clk, rst, enable,
    input wire pause_request,
    output wire paused,
    output reg start,
    output reg read_enable,
    output reg [23:0] payload,
    input wire busy, done, ack_error,
    input wire [7:0] read_data,
    output wire [15:0] status,
    output wire failed
);
    localparam [3:0] IDLE=0, HALT=1, LOAD=2, REG_INIT=3, MCU_START=4,
        WAIT_READY=5, ARM_RELEASE=6, RELEASE=7, WAIT_RELEASE=8,
        ARM_CONT=9, CONT=10, WAIT_CONT=11, RUN=12, ERROR=15;
    reg [3:0] state, error_code;
    reg [7:0] last_read;
    reg [11:0] fw_index;
    reg [2:0] reg_index;
    reg [10:0] poll_count;
    reg [31:0] delay_count;
    reg waiting;
    (* rom_style = "block" *) reg [7:0] firmware [0:FW_BYTES-1];
    reg [7:0] fw_byte;
    initial $readmemh(FW_MEM_FILE, firmware);
    // Registered read infers a synchronous ROM. Issue delay allows address settling.
    always @(posedge clk) fw_byte <= firmware[fw_index];
    assign status = {state,error_code,last_read};
    assign failed = (state == ERROR);
    // A disconnected pause input preserves all legacy instantiations. Never
    // suppress completion of an issued command or pause firmware/mailbox init.
    wire pause_run = (pause_request === 1'b1) && (state == RUN || state == ERROR);
    assign paused = pause_run && !waiting && !start && !busy && !done;
    always @* begin
        payload = 0;
        read_enable = 0;
        case(state)
            HALT: payload=24'h300020;
            LOAD: payload={16'h8000+{4'd0,fw_index},fw_byte};
            REG_INIT: payload={16'h3022+{13'd0,reg_index},(reg_index==7 ? 8'h7f : 8'h00)};
            MCU_START: payload=24'h300000;
            WAIT_READY,RUN: begin payload=24'h302900; read_enable=1; end
            ARM_RELEASE,ARM_CONT: payload=24'h302301;
            RELEASE: payload=24'h302208;
            CONT: payload=24'h302204;
            WAIT_RELEASE,WAIT_CONT: begin payload=24'h302300; read_enable=1; end
            default: begin payload=0; read_enable=0; end
        endcase
    end
    always @(posedge clk) begin
        if(rst) begin
            state<=IDLE; error_code<=0; last_read<=0; fw_index<=0;
            reg_index<=0; poll_count<=0; delay_count<=0; waiting<=0; start<=0;
        end else begin
            start<=0;
            if(state==IDLE) begin
                if(enable) begin state<=HALT; delay_count<=2; end
            end else if(state!=ERROR) begin
                if(waiting) begin
                    if(done) begin
                        waiting<=0;
                        delay_count<=2;
                        if(ack_error) begin state<=ERROR; error_code<=1; end
                        else begin
                            if(read_enable) last_read<=read_data;
                            case(state)
                                HALT: state<=LOAD;
                                LOAD: if(fw_index==FW_BYTES-1) state<=REG_INIT;
                                      else fw_index<=fw_index+1'b1;
                                REG_INIT: if(reg_index==7) state<=MCU_START;
                                          else reg_index<=reg_index+1'b1;
                                MCU_START: begin state<=WAIT_READY; poll_count<=0; delay_count<=POLL_CYCLES; end
                                ARM_RELEASE: state<=RELEASE;
                                RELEASE: begin state<=WAIT_RELEASE; poll_count<=0; delay_count<=POLL_CYCLES; end
                                ARM_CONT: state<=CONT;
                                CONT: begin state<=WAIT_CONT; poll_count<=0; delay_count<=POLL_CYCLES; end
                                WAIT_READY,WAIT_RELEASE,WAIT_CONT: begin
                                    if((state==WAIT_READY && read_data==8'h70) ||
                                       (state!=WAIT_READY && read_data==0)) begin
                                        poll_count<=0;
                                        case(state)
                                            WAIT_READY: state<=ARM_RELEASE;
                                            WAIT_RELEASE: state<=ARM_CONT;
                                            default: begin state<=RUN; delay_count<=POLL_CYCLES; end
                                        endcase
                                    end else if(poll_count==MAX_POLLS-1) begin
                                        state<=ERROR;
                                        error_code<=(state==WAIT_READY)?4'd2:4'd3;
                                    end else begin poll_count<=poll_count+1'b1; delay_count<=POLL_CYCLES; end
                                end
                                RUN: delay_count<=POLL_CYCLES;
                                default: begin state<=ERROR; error_code<=4; end
                            endcase
                        end
                    end
                end else if(delay_count!=0) delay_count<=delay_count-1'b1;
                else if(!busy && !done && !pause_run) begin start<=1; waiting<=1; end
            end
        end
    end
endmodule
