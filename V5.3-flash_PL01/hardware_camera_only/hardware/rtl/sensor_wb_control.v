`timescale 1ns/1ps
// One transaction at a time on camera_config_v02's existing SCCB engine.
// Read operations never write/freeze the sensor. Gain banks are sampled twice;
// equality establishes repeated-bank stability, not a simultaneous sensor latch.
module sensor_wb_control #(
    parameter integer SCCB_TIMEOUT_CYCLES = 100000,
    parameter integer FRAME_TIMEOUT_CYCLES = 25000000,
    parameter integer AF_WAIT_TIMEOUT_CYCLES = 1500000000
)(
    input wire clk50, rst, abort,
    input wire [127:0] request,
    input wire request_valid,
    output reg request_ack,
    output reg [127:0] result,
    output reg result_send,
    input wire result_received,
    output reg pause_request,
    input wire grant, config_done, isp_busy, frame_boundary,
    output reg capture_freeze,
    output wire sccb_start,
    output wire sccb_read,
    output wire [23:0] sccb_payload,
    input wire sccb_busy, sccb_done, sccb_ack_error,
    input wire [7:0] sccb_read_data
);
    localparam [3:0] IDLE=0, ACQUIRE=1, PRE_FRAME=2, ISSUE=3, WAIT_DONE=4,
        DRAIN=5, ADVANCE=6, SETTLE=7, RELEASE=8, RESPOND=9, RESPONSE_END=10,
        FATAL_HOLD=11, ABORT_RESPONSE=12;
    localparam [3:0] READ_ONE=0, BANK_FIRST=1, BANK_SECOND=2, RMW_SAVE=3,
        RMW_WRITE=4, RMW_VERIFY=5, RMW_RESTORE=6, RMW_CHECK=7,
        GAINS_SAVE=8, GAINS_WRITE=9, GAINS_VERIFY=10, GAINS_RESTORE=11,
        GAINS_CHECK=12;
    localparam [31:0] DONE=32'h001, SUCCESS=32'h002, NACK=32'h004,
        TIMEOUT=32'h008, VERIFY_FAIL=32'h010, POLICY_REJECT=32'h020,
        COHERENCY_FAIL=32'h040, ROLLED_BACK=32'h080,
        ROLLBACK_FAILED=32'h100, FATAL=32'h200, DATA_VALID=32'h400;
    reg [3:0] state, operation;
    reg [31:0] sequence_id, flags, wait_count;
    reg [63:0] data;
    reg [15:0] address, bank_address;
    reg [7:0] mask, value;
    reg [11:0] gain_r, gain_g, gain_b;
    reg [7:0] original[0:6], target[0:6];
    reg [47:0] first_bank;
    reg [2:0] index;
    reg [1:0] comparison, frame_count;
    reg mismatch, response_nack, fatal_latched, rollback_failed;
    reg [7:0] response_byte;
    reg start_pending;
    wire abort_asserted = (abort === 1'b1);
    // Suppress even a queued start not yet sampled by the wire engine. The
    // abort input is already synchronized to clk50 by sensor_wb_bridge.
    assign sccb_start=start_pending && !abort_asserted && !fatal_latched;
    wire restore_operation = operation==RMW_RESTORE || operation==RMW_CHECK ||
        operation==GAINS_RESTORE || operation==GAINS_CHECK;
    wire gains_operation = operation>=GAINS_SAVE;
    wire [15:0] transaction_address = gains_operation ? (16'h3400+{13'd0,index}) :
        ((operation==BANK_FIRST || operation==BANK_SECOND) ?
        bank_address+{13'd0,index} : address);
    assign sccb_read = !(operation==RMW_WRITE || operation==RMW_RESTORE ||
        operation==GAINS_WRITE || operation==GAINS_RESTORE);
    assign sccb_payload = {transaction_address,
        restore_operation ? original[index] : target[index]};

    function readable;
        input [15:0] a;
        begin
            readable = a==16'h300a || a==16'h300b || a==16'h4300 ||
                a==16'h501f || a==16'h5000 || a==16'h5001 || a==16'h501d ||
                a==16'h503d || a==16'h4741 ||
                (a>=16'h3400 && a<=16'h3406) || (a>=16'h3500 && a<=16'h350d) ||
                (a>=16'h3a00 && a<=16'h3a1f) || (a>=16'h5180 && a<=16'h51a4) ||
                (a>=16'h5380 && a<=16'h538b) || (a>=16'h5480 && a<=16'h5490) ||
                (a>=16'h5580 && a<=16'h558b) || (a>=16'h5800 && a<=16'h5849);
        end
    endfunction
    function [7:0] writable_mask;
        input [15:0] a;
        begin
            case(a)
                16'h5001,16'h3406: writable_mask=8'h01;
                16'h5196: writable_mask=8'h20;
                16'h3400,16'h3402,16'h3404: writable_mask=8'h0f;
                16'h3401,16'h3403,16'h3405: writable_mask=8'hff;
                default: writable_mask=0;
            endcase
        end
    endfunction
    function [63:0] packed_bank;
        input [47:0] bytes;
        begin packed_bank={16'd0,4'd0,bytes[35:32],bytes[47:40],
            4'd0,bytes[19:16],bytes[31:24],4'd0,bytes[3:0],bytes[15:8]}; end
    endfunction

    // A timed-out transaction may still be running. Never reset it, release its
    // owner, or attempt another write. A full safe-stop/reload is required.
    task terminal_timeout;
        begin
            flags<=(flags & ~(SUCCESS|DATA_VALID))|DONE|TIMEOUT|FATAL|
                (restore_operation?ROLLBACK_FAILED:0);
            data<=0; fatal_latched<=1; pause_request<=1; capture_freeze<=1;
            start_pending<=0; state<=RESPOND;
        end
    endtask
    task finish_operation;
        begin
            frame_count<=0;wait_count<=0;
            if(capture_freeze) state<=SETTLE;
            else state<=RELEASE;
        end
    endtask
    task fail_transaction;
        input [31:0] reason;
        begin
            flags<=flags|reason; data<=0; index<=0;
            case(operation)
                RMW_WRITE,RMW_VERIFY: operation<=RMW_RESTORE;
                GAINS_WRITE,GAINS_VERIFY: operation<=GAINS_RESTORE;
                // Restore all seven bytes even if one restore write NACKs.
                GAINS_RESTORE: begin
                    rollback_failed<=1;
                    if(index==6) begin operation<=GAINS_CHECK;index<=0; end
                    else index<=index+1'b1;
                end
                RMW_RESTORE: begin rollback_failed<=1;operation<=RMW_CHECK;end
                RMW_CHECK: begin
                    flags<=flags|reason|ROLLBACK_FAILED|FATAL;
                    fatal_latched<=1;state<=RESPOND;
                end
                GAINS_CHECK: begin
                    rollback_failed<=1;
                    if(index==6) begin
                        flags<=flags|reason|ROLLBACK_FAILED|FATAL;
                        fatal_latched<=1;state<=RESPOND;
                    end else index<=index+1'b1;
                end
                default: finish_operation;
            endcase
        end
    endtask

    integer k;
    always @(posedge clk50) begin
        if(rst) begin
            state<=IDLE;operation<=READ_ONE;request_ack<=0;result<=0;result_send<=0;
            pause_request<=0;capture_freeze<=0;start_pending<=0;
            sequence_id<=0;flags<=0;data<=0;wait_count<=0;address<=0;bank_address<=0;
            mask<=0;value<=0;gain_r<=0;gain_g<=0;gain_b<=0;first_bank<=0;
            index<=0;comparison<=0;frame_count<=0;mismatch<=0;response_nack<=0;
            response_byte<=0;fatal_latched<=0;rollback_failed<=0;
            for(k=0;k<7;k=k+1) begin original[k]<=0;target[k]<=0;end
        end else begin
            start_pending<=0;
            if(!request_valid) request_ack<=0;
            // Transport failure outranks ISSUE and RELEASE. A clock stall may
            // delay synchronization of abort; after it arrives, no new transfer
            // can launch. An existing wire transfer may drain without reset.
            if(abort_asserted && !fatal_latched) begin
                fatal_latched<=1;pause_request<=1;capture_freeze<=1;data<=0;
                flags<=(flags & ~(SUCCESS|DATA_VALID))|DONE|TIMEOUT|FATAL|
                    (restore_operation?ROLLBACK_FAILED:0);
                // A published bundle belongs to the CDC handshake until ACK.
                // The endpoint is already fatal and will discard its late
                // contents. Do not retract/change it or publish a second packet.
                if(result_send) begin
                    if(result_received) begin result_send<=0;state<=RESPONSE_END;end
                    else state<=ABORT_RESPONSE;
                end else if(state==RESPONSE_END) state<=RESPONSE_END;
                else begin
                    result<={sequence_id,(flags & ~(SUCCESS|DATA_VALID))|DONE|TIMEOUT|FATAL|
                        (restore_operation?ROLLBACK_FAILED:32'd0),64'd0};
                    result_send<=0;
                    if(state==IDLE) state<=FATAL_HOLD;
                    else state<=RESPOND;
                end
            end else case(state)
                IDLE: if(request_valid && !request_ack) begin
                    request_ack<=1; sequence_id<=request[127:96];data<=0;flags<=DONE;
                    address<=request[23:8];mask<=request[31:24];value<=request[39:32];
                    gain_r<=request[51:40];gain_g<=request[63:52];gain_b<=request[75:64];
                    index<=0;comparison<=0;mismatch<=0;rollback_failed<=0;
                    wait_count<=0;frame_count<=0;
                    if(request[95:76]!=0 ||
                       !((request[7:0]==8'h01 && readable(request[23:8])) ||
                         request[7:0]==8'h02 || request[7:0]==8'h03 ||
                         (request[7:0]==8'h10 && request[31:24]!=0 &&
                          (request[31:24] & ~writable_mask(request[23:8]))==0) ||
                         (request[7:0]==8'h11 && request[51:40]>=12'h100 &&
                          request[63:52]>=12'h100 && request[75:64]>=12'h100))) begin
                        flags<=DONE|POLICY_REJECT;state<=RESPOND;
                    end else begin
                        pause_request<=1;
                        capture_freeze<=request[7:0]==8'h10 || request[7:0]==8'h11;
                        case(request[7:0])
                            8'h01: operation<=READ_ONE;
                            8'h02,8'h03: begin
                                operation<=BANK_FIRST;
                                bank_address<=request[7:0]==8'h02 ? 16'h519f : 16'h3400;
                            end
                            8'h10: operation<=RMW_SAVE;
                            default: operation<=GAINS_SAVE;
                        endcase
                        state<=ACQUIRE;
                    end
                end
                ACQUIRE: begin
                    if(grant && config_done && !isp_busy) begin
                        wait_count<=0;frame_count<=0;
                        state<=capture_freeze ? PRE_FRAME : ISSUE;
                    end else if(wait_count==AF_WAIT_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                PRE_FRAME,SETTLE: begin
                    if(frame_boundary) begin
                        wait_count<=0;
                        if((state==PRE_FRAME && frame_count==1) ||
                           (state==SETTLE && frame_count==2)) begin
                            frame_count<=0;state<=(state==PRE_FRAME)?ISSUE:RELEASE;
                        end else frame_count<=frame_count+1'b1;
                    end else if(wait_count==FRAME_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                ISSUE: begin
                    if(grant && !sccb_busy && !sccb_done) begin
                        start_pending<=1;wait_count<=0;state<=WAIT_DONE;
                    end else if(wait_count==SCCB_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                WAIT_DONE: begin
                    if(sccb_done) begin
                        response_byte<=sccb_read_data;response_nack<=sccb_ack_error;
                        wait_count<=0;state<=DRAIN;
                    end else if(wait_count==SCCB_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                DRAIN: begin
                    if(!sccb_busy && !sccb_done) begin wait_count<=0;state<=ADVANCE;end
                    else if(wait_count==SCCB_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                ADVANCE: begin
                    state<=ISSUE;wait_count<=0;
                    if(response_nack) fail_transaction(NACK);
                    else case(operation)
                        READ_ONE: begin
                            data<={56'd0,response_byte};flags<=DONE|SUCCESS|DATA_VALID;
                            finish_operation;
                        end
                        BANK_FIRST: begin
                            first_bank[index*8 +:8]<=response_byte;
                            if(index==5) begin index<=0;mismatch<=0;operation<=BANK_SECOND;end
                            else index<=index+1'b1;
                        end
                        BANK_SECOND: begin
                            if(response_byte!=first_bank[index*8 +:8]) mismatch<=1;
                            if(index==5) begin
                                index<=0;
                                if(mismatch || response_byte!=first_bank[47:40]) begin
                                    if(comparison==3) begin flags<=DONE|COHERENCY_FAIL;finish_operation;end
                                    else begin comparison<=comparison+1'b1;operation<=BANK_FIRST;end
                                end else begin
                                    data<=packed_bank(first_bank);flags<=DONE|SUCCESS|DATA_VALID;
                                    finish_operation;
                                end
                            end else index<=index+1'b1;
                        end
                        RMW_SAVE: begin
                            original[0]<=response_byte;target[0]<=(response_byte & ~mask)|(value & mask);
                            operation<=RMW_WRITE;
                        end
                        RMW_WRITE: operation<=RMW_VERIFY;
                        RMW_VERIFY: begin
                            if(response_byte!=target[0]) fail_transaction(VERIFY_FAIL);
                            else begin
                                data<={40'd0,response_byte,target[0],original[0]};
                                flags<=DONE|SUCCESS|DATA_VALID;finish_operation;
                            end
                        end
                        RMW_RESTORE: operation<=RMW_CHECK;
                        RMW_CHECK: begin
                            if(response_byte!=original[0]) fail_transaction(VERIFY_FAIL);
                            else if(rollback_failed) begin
                                flags<=flags|ROLLBACK_FAILED|FATAL;fatal_latched<=1;state<=RESPOND;
                            end else begin flags<=flags|ROLLED_BACK;finish_operation;end
                        end
                        GAINS_SAVE: begin
                            original[index]<=response_byte;
                            case(index)
                                0: target[0]<={response_byte[7:4],gain_r[11:8]};
                                1: target[1]<=gain_r[7:0];
                                2: target[2]<={response_byte[7:4],gain_g[11:8]};
                                3: target[3]<=gain_g[7:0];
                                4: target[4]<={response_byte[7:4],gain_b[11:8]};
                                5: target[5]<=gain_b[7:0];
                                6: target[6]<=response_byte|8'h01;
                            endcase
                            if(index==6) begin index<=6;operation<=GAINS_WRITE;end
                            else index<=index+1'b1;
                        end
                        GAINS_WRITE: begin
                            if(index==6) index<=0; // Manual mode before any gain.
                            else if(index==5) begin index<=0;operation<=GAINS_VERIFY;end
                            else index<=index+1'b1;
                        end
                        GAINS_VERIFY: begin
                            if(response_byte!=target[index]) fail_transaction(VERIFY_FAIL);
                            else if(index==6) begin
                                // All seven bytes, including preserved bits, were read back.
                                data<=packed_bank({target[5],target[4],target[3],target[2],target[1],target[0]});
                                flags<=DONE|SUCCESS|DATA_VALID;finish_operation;
                            end else index<=index+1'b1;
                        end
                        GAINS_RESTORE: begin
                            // Restore the original mode last, after all original gains.
                            if(index==6) begin index<=0;operation<=GAINS_CHECK;end
                            else index<=index+1'b1;
                        end
                        GAINS_CHECK: begin
                            if(response_byte!=original[index]) fail_transaction(VERIFY_FAIL);
                            else if(index==6) begin
                                if(rollback_failed) begin
                                    flags<=flags|ROLLBACK_FAILED|FATAL;fatal_latched<=1;state<=RESPOND;
                                end else begin flags<=flags|ROLLED_BACK;finish_operation;end
                            end else index<=index+1'b1;
                        end
                        default: terminal_timeout;
                    endcase
                end
                RELEASE: begin
                    // The last done was drained before ADVANCE; guard again before
                    // returning ownership. AF never receives this command's done.
                    if(!sccb_busy && !sccb_done) begin
                        pause_request<=0;capture_freeze<=0;state<=RESPOND;
                    end else if(wait_count==SCCB_TIMEOUT_CYCLES-1) terminal_timeout;
                    else wait_count<=wait_count+1'b1;
                end
                RESPOND: begin
                    result<={sequence_id,flags,data};result_send<=1;
                    if(result_send && result_received) begin result_send<=0;state<=RESPONSE_END;end
                end
                ABORT_RESPONSE: if(result_received) begin result_send<=0;state<=RESPONSE_END;end
                RESPONSE_END: if(!result_received) state<=fatal_latched?FATAL_HOLD:IDLE;
                FATAL_HOLD: begin pause_request<=1;capture_freeze<=1;end
                default: terminal_timeout;
            endcase
        end
    end
endmodule
