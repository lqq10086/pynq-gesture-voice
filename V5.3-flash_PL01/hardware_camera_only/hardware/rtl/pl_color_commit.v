`timescale 1ns/1ps
// Acknowledge a particular accepted frame, not equality with an old mode.
// Counting SOFs covers arbitrary stalls and even tiny frames in simulation.
module pl_color_commit(
 input wire clk50,pix_clk,pix_reset,target,send,output wire received,
 input wire upstream_sof,upstream_mode,downstream_sof
);
 wire requested,target_pix;reg acknowledged;
 xpm_cdc_handshake #(.WIDTH(1),.DEST_EXT_HSK(1),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),.INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) frame_cdc_i(
  .src_clk(clk50),.src_in(target),.src_send(send),.src_rcv(received),
  .dest_clk(pix_clk),.dest_out(target_pix),.dest_req(requested),.dest_ack(acknowledged));
 reg [15:0] input_frames,output_frames,selected_frame;
 reg selected;
 always @(posedge pix_clk)begin
  if(pix_reset)begin
   input_frames<=0;output_frames<=0;selected_frame<=0;selected<=0;acknowledged<=0;
  end else begin
   if(upstream_sof)input_frames<=input_frames+1'b1;
   if(downstream_sof)output_frames<=output_frames+1'b1;
   if(!requested)begin acknowledged<=0;selected<=0;end
   else if(!acknowledged)begin
    if(!selected && upstream_sof && upstream_mode==target_pix)begin
     selected<=1;selected_frame<=input_frames+1'b1;
     if(downstream_sof && output_frames==input_frames)acknowledged<=1;
    end else if(selected && downstream_sof && (output_frames+16'd1)==selected_frame)
     acknowledged<=1;
   end
  end
 end
endmodule
