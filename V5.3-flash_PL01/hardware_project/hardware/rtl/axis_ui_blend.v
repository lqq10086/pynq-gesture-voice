`timescale 1ns/1ps
// Camera ready depends ONLY on the downstream video path. Missing UI data
// invalidates this UI frame, never stalls the camera. The 16-bit nonzero tag
// travels with every output pixel to the physical display completion check.
module axis_ui_blend #(
 parameter integer WIDTH=1280
)(
 input wire clk, input wire rst,
 input wire [15:0] s_axis_tdata,
 input wire s_axis_tvalid, output wire s_axis_tready,
 input wire s_axis_tuser, input wire s_axis_tlast,
 input wire [63:0] ui_tdata,
 input wire ui_tvalid, output wire ui_tready,
 input wire ui_tuser, input wire ui_tlast,
 output wire [15:0] m_axis_tdata, output wire [15:0] m_axis_tag,
 output wire m_axis_tvalid, input wire m_axis_tready,
 output wire m_axis_tuser, output wire m_axis_tlast,
 output reg [31:0] rejected_frames
);
 reg aligned;
 reg [15:0] frame_tag;
 reg [$clog2(WIDTH)-1:0] x;
 reg [3:0] valid_pipe, user_pipe, last_pipe;
 reg [15:0] tag0,tag1,tag2,tag3;
 reg [15:0] prod_b,prod_g,prod_r;
 reg [16:0] round_b,round_g,round_r;
 reg [7:0] p0_b,p0_g,p0_r,p1_b,p1_g,p1_r;
 reg [8:0] sum_b,sum_g,sum_r;
 reg [15:0] packed_pixel;
 wire advance = !valid_pipe[3] || m_axis_tready;
 wire take = s_axis_tvalid && s_axis_tready;
 wire [15:0] wanted_tag = s_axis_tuser ? ui_tdata[63:48] : frame_tag;
 wire good = ui_tvalid && (s_axis_tuser || aligned) &&
             (ui_tuser == s_axis_tuser) && (ui_tlast == s_axis_tlast) &&
             (ui_tdata[63:48] == wanted_tag) && (wanted_tag != 0) &&
             (s_axis_tlast == (x == WIDTH-1));
 // Unaligned UI is drained until its next SOF, which is held for camera SOF.
 assign ui_tready = !rst && ((take && (s_axis_tuser || aligned)) ||
                            (!aligned && ui_tvalid && !ui_tuser));
 assign s_axis_tready = !rst && advance;
 assign m_axis_tvalid = valid_pipe[3];
 assign m_axis_tuser = user_pipe[3];
 assign m_axis_tlast = last_pipe[3];
 assign m_axis_tdata = packed_pixel;
 assign m_axis_tag = tag3;
 wire [7:0] cb = {s_axis_tdata[4:0],s_axis_tdata[4:2]};
 wire [7:0] cg = {s_axis_tdata[10:5],s_axis_tdata[10:9]};
 wire [7:0] cr = {s_axis_tdata[15:11],s_axis_tdata[15:13]};
 wire [7:0] out_b = sum_b[8] ? 8'hff : sum_b[7:0];
 wire [7:0] out_g = sum_g[8] ? 8'hff : sum_g[7:0];
 wire [7:0] out_r = sum_r[8] ? 8'hff : sum_r[7:0];
 wire [16:0] div_b = round_b + (round_b >> 8);
 wire [16:0] div_g = round_g + (round_g >> 8);
 wire [16:0] div_r = round_r + (round_r >> 8);
 always @(posedge clk) begin
  if(rst) begin
   aligned<=0; frame_tag<=0; x<=0; rejected_frames<=0;
   valid_pipe<=0; user_pipe<=0; last_pipe<=0;
   tag0<=0;tag1<=0;tag2<=0;tag3<=0;
   prod_b<=0;prod_g<=0;prod_r<=0;
   round_b<=0;round_g<=0;round_r<=0;
   p0_b<=0;p0_g<=0;p0_r<=0;p1_b<=0;p1_g<=0;p1_r<=0;
   sum_b<=0;sum_g<=0;sum_r<=0;packed_pixel<=0;
  end else begin
   if(take) begin
    if(s_axis_tlast) x<=0; else x<=x+1'b1;
    if(s_axis_tuser) begin
     frame_tag<=ui_tdata[63:48]; aligned<=good;
     if(!good) rejected_frames<=rejected_frames+1'b1;
    end else if(aligned && !good) begin
     aligned<=0; rejected_frames<=rejected_frames+1'b1;
    end
   end
   if(advance) begin
    valid_pipe<={valid_pipe[2:0],s_axis_tvalid};
    user_pipe<={user_pipe[2:0],s_axis_tuser};
    last_pipe<={last_pipe[2:0],s_axis_tlast};
    tag0<=good ? wanted_tag : 16'd0;tag1<=tag0;tag2<=tag1;tag3<=tag2;
    prod_b<=cb*(good ? ui_tdata[31:24] : 8'd255);
    prod_g<=cg*(good ? ui_tdata[39:32] : 8'd255);
    prod_r<=cr*(good ? ui_tdata[47:40] : 8'd255);
    p0_b<=good ? ui_tdata[7:0] : 8'd0;
    p0_g<=good ? ui_tdata[15:8] : 8'd0;
    p0_r<=good ? ui_tdata[23:16] : 8'd0;
    round_b<={1'b0,prod_b}+17'd128;
    round_g<={1'b0,prod_g}+17'd128;
    round_r<={1'b0,prod_r}+17'd128;
    p1_b<=p0_b;p1_g<=p0_g;p1_r<=p0_r;
    sum_b<={1'b0,div_b[15:8]}+{1'b0,p1_b};
    sum_g<={1'b0,div_g[15:8]}+{1'b0,p1_g};
    sum_r<={1'b0,div_r[15:8]}+{1'b0,p1_r};
    packed_pixel<={out_r[7:3],out_g[7:2],out_b[7:3]};
   end
  end
 end
endmodule
