`timescale 1ns/1ps
// Breaks caught: invalid bus writes, torn gains, lost original bytes, unsafe
// release after timeout, wrong seven-byte write/restore order and stale results.
module tb_sensor_wb_control;
  reg clk=0; always #10 clk=~clk;
  reg rst=1, abort=0, request_valid=0, result_received=0;
  reg [127:0] request=0;
  wire request_ack, result_send, pause_request, capture_freeze;
  wire [127:0] result;
  reg grant_enable=1, config_done=1, isp_busy=0, frames_enable=1;
  wire grant=pause_request && grant_enable;
  reg frame_boundary=0;
  wire sccb_start,sccb_read;
  wire [23:0] sccb_payload;
  reg sccb_busy=0,sccb_done=0,sccb_ack_error=0;
  reg [7:0] sccb_read_data=0;
  sensor_wb_control #(.SCCB_TIMEOUT_CYCLES(30),.FRAME_TIMEOUT_CYCLES(100),
    .AF_WAIT_TIMEOUT_CYCLES(100)) dut(.*,.clk50(clk));
  reg [7:0] mem[0:65535];
  reg [15:0] addresses[0:255]; reg [7:0] values[0:255]; reg reads[0:255];
  integer cycles=0,frame_cycle=0,tx=0,writes=0,reads_count=0,timer=0;
  integer nack_at=0,nack2_at=0,corrupt_at=0,hang_at=0,chaos=0,seq=0,disable_frames_at=0;
  integer latched_tx=0,done_hold=0,hold_done_cycles=1;
  reg [23:0] latched_payload; reg latched_read;
  reg [127:0] saved_result;
  reg monitor_freeze=0; integer pre_edges=0,write_last_cycle=0,post_edges=0;

  always @(posedge clk) begin
    cycles<=cycles+1;
    frame_boundary<=0;
    if(frame_cycle==9) begin
      frame_cycle<=0;
      if(frames_enable) frame_boundary<=1;
    end else frame_cycle<=frame_cycle+1;
    if(capture_freeze && frame_boundary) begin
      pre_edges=pre_edges+1;
      if(write_last_cycle!=0) post_edges=post_edges+1;
    end
    if(rst) begin
      sccb_busy<=0; sccb_done<=0; timer<=0; done_hold<=0;
    end else begin
      if(sccb_done) begin
        if(done_hold==0) sccb_done<=0;
        else done_hold<=done_hold-1;
      end
      if(sccb_start) begin
        if(!grant || sccb_busy || sccb_done) $fatal(1,"unsafe SCCB launch");
        tx=tx+1; addresses[tx-1]=sccb_payload[23:8];
        values[tx-1]=sccb_payload[7:0]; reads[tx-1]=sccb_read;
        if(sccb_read) reads_count=reads_count+1;
        else begin
          writes=writes+1;
          if(!capture_freeze || pre_edges<2) $fatal(1,"write before two frozen frame boundaries");
          write_last_cycle=cycles; post_edges=0;
        end
        latched_tx=tx; latched_payload=sccb_payload; latched_read=sccb_read;
        sccb_busy<=1; timer<=3;
      end
      if(sccb_busy && latched_tx!=hang_at) begin
        if(timer!=0) timer<=timer-1;
        else begin
          sccb_busy<=0; sccb_done<=1; done_hold<=hold_done_cycles-1;
          sccb_ack_error<=(latched_tx==nack_at || latched_tx==nack2_at);
          if(latched_tx==disable_frames_at) frames_enable=0;
          // A NACKed write may already have changed the device: rollback must
          // restore even this case; the sequencer cannot infer no side effect.
          if(!latched_read) mem[latched_payload[23:8]]=latched_payload[7:0];
          else if(chaos && latched_payload[23:8]==16'h519f)
            sccb_read_data<=((latched_tx-1)/6)%2 ? 8'ha2 : 8'ha1;
          else sccb_read_data<=mem[latched_payload[23:8]] ^
            ((latched_tx==corrupt_at)?8'h01:8'h00);
        end
      end
    end
  end

  task fresh;
    begin
      @(negedge clk); rst=1; abort=0; request_valid=0; result_received=0;
      repeat(3) @(negedge clk);
      tx=0; writes=0; reads_count=0; nack_at=0; nack2_at=0; corrupt_at=0;
      hang_at=0; chaos=0; grant_enable=1; frames_enable=1; hold_done_cycles=1;disable_frames_at=0;
      config_done=1; isp_busy=0; pre_edges=0; post_edges=0; write_last_cycle=0;
      mem[16'h5001]=8'ha3; mem[16'h3400]=8'ha1;mem[16'h3401]=8'h23;
      mem[16'h5196]=8'hc3;
      mem[16'h3402]=8'hb4;mem[16'h3403]=8'h56;
      mem[16'h3404]=8'hc7;mem[16'h3405]=8'h89;mem[16'h3406]=8'ha0;
      mem[16'h519f]=8'ha1;mem[16'h51a0]=8'h23;
      mem[16'h51a1]=8'hb4;mem[16'h51a2]=8'h56;
      mem[16'h51a3]=8'hc7;mem[16'h51a4]=8'h89;
      rst=0;
      repeat(2) @(negedge clk);
    end
  endtask
  task submit(input [7:0] op,input [15:0] addr,input[7:0] mask,
              input[7:0] value,input[11:0] r,input[11:0] g,input[11:0] b);
    integer n;
    begin
      seq=seq+1;
      @(negedge clk);request={seq[31:0],20'd0,b,g,r,value,mask,addr,op};request_valid=1;
      n=0; while(!request_ack && n<500) begin @(negedge clk); n=n+1; end
      if(!request_ack) $fatal(1,"missing request acceptance");
      request_valid=0;
      n=0; while(!result_send && n<20000) begin @(negedge clk); n=n+1; end
      if(!result_send) $fatal(1,"missing bounded completion op=%h tx=%d",op,tx);
      saved_result=result;
      if(result[127:96]!==seq[31:0]) $fatal(1,"wrong sequence");
      repeat(3) begin @(negedge clk); if(result!==saved_result || !result_send) $fatal(1,"unstable response handshake"); end
      result_received=1;repeat(2) @(negedge clk); result_received=0;
      repeat(3) @(negedge clk);
    end
  endtask
  task flags(input[31:0] want);
    begin if(saved_result[95:64]!==want) $fatal(1,"flags got=%h want=%h tx=%d",saved_result[95:64],want,tx); end
  endtask
  task restored;
    begin
      if({mem[16'h3400],mem[16'h3401],mem[16'h3402],mem[16'h3403],mem[16'h3404],mem[16'h3405],mem[16'h3406]}!==56'ha123b456c789a0)
        $fatal(1,"not exact original bank/mode");
    end
  endtask
  task abort_pending_response(input[7:0] op,input[31:0] expected_flags,input integer ack_timing);
    integer c,accepted_transactions;
    begin
      seq=seq+1;
      @(negedge clk);request={seq[31:0],32'd0,32'd0,8'd0,16'h5001,op};request_valid=1;
      c=0;while(!request_ack && c<500) begin @(negedge clk);c=c+1;end
      if(!request_ack) $fatal(1,"pending-response request not accepted");
      request_valid=0;
      c=0;while(!result_send && c<2000) begin @(negedge clk);c=c+1;end
      if(!result_send || result[95:64]!==expected_flags || result[127:96]!==seq[31:0])
        $fatal(1,"pending-response initial packet incorrect");
      if(expected_flags==32'h403 && result[63:0]!==64'ha3) $fatal(1,"pending success byte incorrect");
      saved_result=result;accepted_transactions=tx;
      // ack_timing 0: abort before ACK; 1: abort with ACK; 2: abort after
      // ACK was sampled but before the consumer lowers received.
      if(ack_timing==2) begin
        result_received=1;repeat(2) @(negedge clk);
        if(result_send) $fatal(1,"ordinary ACK did not retire send");
      end
      abort=1;if(ack_timing==1) result_received=1;
      repeat(5) begin
        @(negedge clk);
        if(result!==saved_result || result_send!==(ack_timing==0) ||
           !pause_request || !capture_freeze || sccb_start || tx!=accepted_transactions)
          $fatal(1,"abort changed in-flight response bundle/send or resumed sensor");
      end
      result_received=1;
      repeat(4) begin @(negedge clk);if(result_send || result!==saved_result) $fatal(1,"abort response not drained once");end
      result_received=0;abort=0;
      repeat(10) begin @(negedge clk);if(result_send || result!==saved_result || !pause_request || !capture_freeze)
        $fatal(1,"abort sent a second packet or released after ACK");end
      request_valid=1;repeat(5) @(negedge clk);
      if(request_ack) $fatal(1,"response-abort accepted a later command");request_valid=0;
    end
  endtask

  initial begin
    fresh; hold_done_cycles=3;submit(1,16'h5001,0,0,0,0,0);flags(32'h403);
    if(saved_result[63:0]!==64'ha3 || tx!=1 || writes!=0 || pause_request || capture_freeze) $fatal(1,"READ8 behavior");
    $display("PASS READ8 actual byte and done drain");
    fresh; submit(1,16'h1234,0,0,0,0,0); flags(32'h21); if(tx!=0) $fatal(1,"forbidden READ8 touched bus");
    fresh; submit(8'h10,16'h5001,8'h80,0,0,0,0);flags(32'h21);if(tx!=0) $fatal(1,"forbidden mask touched bus");
    fresh; submit(8'h10,16'h5001,0,0,0,0,0);flags(32'h21);if(tx!=0) $fatal(1,"zero mask touched bus");
    fresh; submit(8'h11,0,0,0,12'h0ff,12'h400,12'h500); flags(32'h21);if(tx!=0) $fatal(1,"gain bound touched bus");
    fresh; submit(8'hff,16'h5001,1,0,0,0,0);flags(32'h21);if(tx!=0) $fatal(1,"unknown command touched bus");
    $display("PASS whitelist, nonzero subset and gain range policy");
    fresh;nack_at=1;submit(1,16'h5001,0,0,0,0,0);flags(32'h5);
    if(saved_result[63:0]!=0 || writes!=0 || pause_request) $fatal(1,"NACK data/release");
    fresh;submit(2,0,0,0,0,0,0);flags(32'h403);
    if(saved_result[63:0]!==64'h0000078904560123 || tx!=12 || writes!=0) $fatal(1,"CURRENT_GAINS packing/two banks");
    fresh;submit(3,0,0,0,0,0,0);flags(32'h403);
    if(saved_result[63:0]!==64'h0000078904560123 || tx!=12 || writes!=0) $fatal(1,"MANUAL_GAINS packing/two banks");
    fresh;corrupt_at=7;submit(2,0,0,0,0,0,0);flags(32'h403);
    if(tx!=24 || saved_result[63:0]!==64'h0000078904560123) $fatal(1,"coherency retry");
    fresh;chaos=1;submit(2,0,0,0,0,0,0);flags(32'h41);
    if(tx!=48 || saved_result[63:0]!=0 || writes!=0) $fatal(1,"bounded coherence failure");
    $display("PASS repeated banks, retry, bounded coherence failure without freeze write");
    fresh;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h403);
    if(mem[16'h5001]!==8'ha2 || saved_result[63:0]!==64'ha2a2a3 || writes!=1 || post_edges<3 || capture_freeze) $fatal(1,"RMW preserve/verify/settle");
    fresh;submit(8'h10,16'h3400,8'h0f,8'h05,0,0,0);flags(32'h403);
    if(mem[16'h3400]!==8'ha5) $fatal(1,"RMW gain high-byte preserved nibble");
    fresh;submit(8'h10,16'h5196,8'h20,8'h20,0,0,0);flags(32'h403);
    if(mem[16'h5196]!==8'he3) $fatal(1,"RMW freeze unrelated bits");
    fresh;corrupt_at=3;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h91);
    if(mem[16'h5001]!==8'ha3 || tx!=5 || saved_result[63:0]!=0) $fatal(1,"RMW mismatch rollback");
    fresh;nack_at=2;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h85);
    if(mem[16'h5001]!==8'ha3 || tx!=4) $fatal(1,"RMW NACK partial write rollback");
    $display("PASS RMW exact original and verified rollback");
    fresh;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h403);
    if({mem[16'h3400],mem[16'h3401],mem[16'h3402],mem[16'h3403],mem[16'h3404],mem[16'h3405],mem[16'h3406]}!==56'ha321b654c987a1)
      $fatal(1,"SET_GAINS nibble/mode preservation");
    if(addresses[7]!==16'h3406 || values[7]!==8'ha1 || reads[7] || tx!=21 || writes!=7 || post_edges<3 || saved_result[63:0]!==64'h0000098706540321)
      $fatal(1,"SET_GAINS order/verified result");
    fresh;corrupt_at=15;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h91);restored;
    if(addresses[21]!==16'h3406 || values[21]!==8'ha0 || reads[21] || tx!=29) $fatal(1,"restore mode must follow six original bytes");
    fresh;nack_at=10;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h85);restored;
    if(tx!=24) $fatal(1,"partial SET_GAINS rollback transaction count");
    fresh;nack_at=3;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h5);restored;
    if(writes!=0) $fatal(1,"snapshot failure caused writes");
    fresh;corrupt_at=21;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h91);restored;
    if(tx!=35) $fatal(1,"final mode verify failure did not restore full bank");
    fresh;nack_at=10;nack2_at=12;submit(8'h11,0,0,0,12'h321,12'h654,12'h987);flags(32'h305);restored;
    if(tx!=24 || !pause_request || !capture_freeze) $fatal(1,"failed gain rollback must complete attempts and retain owner");
    $display("PASS SET_GAINS save/write/verify/restore and failed snapshot");
    fresh;corrupt_at=3;nack_at=4;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h315);
    if(!pause_request || !capture_freeze || saved_result[63:0]!=0) $fatal(1,"rollback failure must hold fatal");
    fresh;hang_at=1;submit(1,16'h5001,0,0,0,0,0);flags(32'h209);
    if(!pause_request || !capture_freeze || !sccb_busy) $fatal(1,"timeout released live engine");
    hang_at=0;repeat(100) @(negedge clk);
    if(result!==saved_result || !pause_request || !capture_freeze) $fatal(1,"late done cleared fatal result");
    request_valid=1;repeat(20) @(negedge clk);if(request_ack) $fatal(1,"terminal state accepted request");request_valid=0;
    fresh;grant_enable=0;submit(1,16'h5001,0,0,0,0,0);flags(32'h209);if(tx!=0) $fatal(1,"grant timeout touched engine");
    fresh;frames_enable=0;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h209);if(tx!=0 || !capture_freeze) $fatal(1,"preframe timeout touched sensor");
    fresh;disable_frames_at=3;submit(8'h10,16'h5001,1,0,0,0,0);flags(32'h209);
    if(saved_result[63:0]!=0 || !capture_freeze || !pause_request || mem[16'h5001]!==8'ha2)
      $fatal(1,"settle timeout must revoke successful data and retain uncertainty");
    fresh;
    fork
      submit(1,16'h5001,0,0,0,0,0);
      begin wait(sccb_start);@(negedge clk);abort=1;end
    join
    flags(32'h209);if(tx!=0 || !pause_request || !capture_freeze) $fatal(1,"abort allowed queued start");
    fresh;
    fork
      submit(8'h10,16'h5001,1,0,0,0,0);
      begin wait(writes==1 && sccb_busy);@(negedge clk);abort=1;end
    join
    flags(32'h209);abort=0;repeat(100) @(negedge clk);
    if(tx!=2 || writes!=1 || sccb_busy || !pause_request || !capture_freeze || result!==saved_result)
      $fatal(1,"abort must let inflight drain but never verify/write/release or recover");
    fresh;
    fork
      submit(8'h10,16'h5001,1,0,0,0,0);
      begin wait(tx==3);wait(!sccb_busy && !sccb_done);repeat(4) @(negedge clk);abort=1;end
    join
    flags(32'h209);if(saved_result[63:0]!=0 || !capture_freeze || !pause_request) $fatal(1,"abort retained success during settle");
    fresh;
    fork
      submit(8'h10,16'h5001,1,0,0,0,0);
      begin wait(dut.state==8);@(negedge clk);abort=1;end
    join
    flags(32'h209);if(!capture_freeze || !pause_request) $fatal(1,"abort lost same-cycle RELEASE priority");
    fresh;abort=1;repeat(6) @(negedge clk);abort=0;
    request_valid=1;repeat(10) @(negedge clk);
    if(request_ack || result_send || sccb_start || !pause_request || !capture_freeze)
      $fatal(1,"idle abort invented completion or accepted later request");
    request_valid=0;
    fresh;abort_pending_response(1,32'h403,0);
    fresh;abort_pending_response(8'hff,32'h21,0);
    fresh;nack_at=1;abort_pending_response(1,32'h5,0);
    fresh;abort_pending_response(1,32'h403,1);
    fresh;abort_pending_response(1,32'h403,2);
    $display("PASS abort preserves pending success/error bundle until ACK without second packet");
    $display("PASS external abort queued/inflight/settle/idle and permanent fail closed");
    $display("PASS timeout, failed rollback, terminal late completion and frame guard");
    $display("PASS tb_sensor_wb_control"); $finish;
  end
  initial begin #10000000; $fatal(1,"testbench watchdog");end
endmodule

`ifdef WB_RED_STUB
// Deliberately absent feature used only to establish a behavioral red run before
// the new production module exists. It accepts no request and performs no I/O.
module sensor_wb_control #(parameter SCCB_TIMEOUT_CYCLES=30,FRAME_TIMEOUT_CYCLES=100,AF_WAIT_TIMEOUT_CYCLES=100)(
 input clk50,rst,input[127:0] request,input request_valid,output request_ack,
 output[127:0] result,output result_send,input result_received,
 output pause_request,input grant,config_done,isp_busy,frame_boundary,output capture_freeze,
 output sccb_start,sccb_read,output[23:0] sccb_payload,input sccb_busy,sccb_done,sccb_ack_error,input[7:0]sccb_read_data);
 wire[3:0]state=0;
 assign request_ack=0;assign result=0;assign result_send=0;assign pause_request=0;
 assign capture_freeze=0;assign sccb_start=0;assign sccb_read=0;assign sccb_payload=0;
endmodule
`endif
