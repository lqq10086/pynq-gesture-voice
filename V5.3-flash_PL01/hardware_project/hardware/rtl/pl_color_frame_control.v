`timescale 1ns/1ps
// Freeze the common Run05 selection at upstream SOF. The spatial stage has
// five registered stages, giving the unchanged global stage's two-FF request
// synchronizer time to settle before that same SOF reaches it.
module pl_color_frame_control(
 input wire clk,rst,request_async,valid,ready,sof,
 output wire spatial_request,global_request,requested);
 (* ASYNC_REG="TRUE" *) reg [1:0] request_sync;
 reg frame_mode;
 assign requested=request_sync[1];
 assign spatial_request=(valid && sof)?request_sync[1]:frame_mode;
 assign global_request=frame_mode;
 always @(posedge clk) begin
  if(rst) begin request_sync<=0;frame_mode<=0;end
  else begin
   request_sync<={request_sync[0],request_async};
   if(valid && ready && sof) frame_mode<=request_sync[1];
  end
 end
endmodule
