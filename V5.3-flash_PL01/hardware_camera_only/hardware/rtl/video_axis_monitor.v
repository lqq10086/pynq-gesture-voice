`timescale 1ns/1ps
// Passive observer: no signal feeds back into the video datapath.
module video_axis_monitor #(parameter integer WIDTH=1280,HEIGHT=720)(
 input wire clk, input wire reset,
 input wire valid,ready,sof,eol,input wire [1:0] keep,
 output reg [31:0] beats=0,sofs=0,eols=0,errors=0,stalls=0
);
 reg [15:0] x=0,y=0;
 reg active=0;
 wire first=sof;
 wire [15:0] px=first?16'd0:x;
 wire [15:0] py=first?16'd0:y;
 wire bad=(keep!=2'b11) || (sof && active) || (!sof && !active) ||
          (eol!=(px==WIDTH-1));
 always @(posedge clk) begin
  if(reset) begin x<=0;y<=0;active<=0;end
  else begin
   if(valid&&!ready) stalls<=stalls+1'b1;
   if(valid&&ready) begin
    beats<=beats+1'b1;
    if(sof) sofs<=sofs+1'b1;
    if(eol) eols<=eols+1'b1;
    if(bad) errors<=errors+1'b1;
    if(sof || active) begin
     if(eol) begin
      x<=0;
      if(py==HEIGHT-1) begin y<=0;active<=0;end
      else begin y<=py+1'b1;active<=1;end
     end else begin x<=px+1'b1;y<=py;active<=1;end
    end
   end
  end
 end
endmodule
