`timescale 1ns/1ps
// Fixed 1280x720 RGB565, packed stride 2560, 8-byte aligned full writes.
// All AWIDs/BIDs must be zero. The marker FIFO associates each accepted AW
// with its ordered AXI B response. Only a clean final B confirms a DDR frame.
// Any geometry, ordering, strobe or FIFO fault disables completion counting
// until the VDMA reset is observed; the fault remains visible for that epoch.
module s2mm_write_observer #(
 parameter integer FRAME_BYTES=1843200, TAG_DEPTH=64
)(
 input wire clk,reset,
 input wire [31:0] awaddr,input wire [7:0] awlen,input wire [2:0] awsize,
 input wire [1:0] awburst,input wire awid,awvalid,awready,
 input wire [7:0] wstrb,input wire wvalid,wready,
 input wire bid,input wire [1:0] bresp,input wire bvalid,bready,
 output reg [31:0] aw_count=0,b_count=0,complete_count=0,error_count=0,
 output reg [31:0] fault=0,output wire [31:0] pending
);
 localparam integer PTR=$clog2(TAG_DEPTH);
 reg tags[0:TAG_DEPTH-1];
 reg [PTR-1:0] wr=0,rd=0;
 reg [PTR:0] count=0;
 reg [31:0] frame_bytes=0,next_addr=0;
 wire aw=awvalid&&awready;
 wire br=bvalid&&bready;
 wire [31:0] burst_bytes=({24'd0,awlen}+32'd1)<<awsize;
 wire [31:0] sum_bytes=frame_bytes+burst_bytes;
 wire aw_bad=awid || awsize!=3 || awburst!=1 || awaddr[2:0]!=0 ||
   (frame_bytes!=0 && awaddr!=next_addr) || sum_bytes>FRAME_BYTES;
 wire overflow=aw && count==TAG_DEPTH && !br;
 wire underflow=br && count==0;
 wire response_error=br && (bid || bresp!=0);
 wire strobe_error=wvalid&&wready && wstrb!=8'hff;
 wire any_error=(aw&&aw_bad)||overflow||underflow||response_error||strobe_error;
 assign pending=count;
 always @(posedge clk) begin
  if(reset) begin wr<=0;rd<=0;count<=0;frame_bytes<=0;next_addr<=0;fault<=0;end
  else begin
   if(aw) aw_count<=aw_count+1'b1;
   if(br) b_count<=b_count+1'b1;
   if(any_error) begin
    error_count<=error_count+1'b1;
    fault<=fault | {27'd0,strobe_error,response_error,underflow,overflow,(aw&&aw_bad)};
   end
   if(fault==0 && !any_error) begin
    if(aw) begin
     tags[wr]<=(sum_bytes==FRAME_BYTES);wr<=wr+1'b1;
     next_addr<=awaddr+burst_bytes;
     frame_bytes<=(sum_bytes==FRAME_BYTES)?32'd0:sum_bytes;
    end
    if(br) begin
     rd<=rd+1'b1;
     if(tags[rd]) complete_count<=complete_count+1'b1;
    end
    case({aw,br})
     2'b10:count<=count+1'b1;
     2'b01:count<=count-1'b1;
     default:count<=count;
    endcase
   end
  end
 end
endmodule
