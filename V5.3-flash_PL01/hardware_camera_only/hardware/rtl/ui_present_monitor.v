`timescale 1ns/1ps
// Observe the actual RGB/timing signals entering TMDS. A fence acknowledges
// only the last active pixel of a complete, consistently tagged HDMI frame.
module ui_present_monitor #(
 parameter integer WIDTH=1280, parameter integer HEIGHT=720
)(
 input wire clk,input wire rst,input wire de,input wire frame_start,
 input wire [15:0] pixel_tag,
 output reg [15:0] presented_tag,
 output reg [31:0] presented_count,
 output reg [31:0] video_count
);
 reg [15:0] active_tag;
 reg frame_good;
 reg [$clog2(WIDTH*HEIGHT)-1:0] pixels;
 always @(posedge clk) begin
  if(rst) begin
   presented_tag<=0;presented_count<=0;video_count<=0;
   active_tag<=0;frame_good<=0;pixels<=0;
  end else if(frame_start) begin
   active_tag<=pixel_tag;frame_good<=de && pixel_tag!=0;
   pixels<=de ? 1 : 0;
  end else if(de) begin
   if(pixel_tag!=active_tag || pixel_tag==0) frame_good<=0;
   if(pixels==WIDTH*HEIGHT-1) begin
    video_count<=video_count+1'b1;
    if(frame_good && pixel_tag==active_tag && pixel_tag!=0) begin
     presented_tag<=active_tag;presented_count<=presented_count+1'b1;
    end
   end
   pixels<=pixels+1'b1;
  end
 end
endmodule
