`timescale 1ns/1ps
module video_stats_snapshot #(
 parameter integer WIDTH=1280,HEIGHT=720,TIMEOUT_CYCLES=16777216
)(
 input wire axi_clk, input wire request, input wire [7:0] read_addr,
 output reg [31:0] read_data, output wire request_ready,
 input wire cam_clk,cam_reset,cam_vsync,cam_done,
 input wire [1:0] cam_drop_events,input wire cam_overflow_event,
 input wire cam_valid,cam_ready,cam_sof,cam_eol,input wire [1:0] cam_keep,
 input wire mem_clk,mem_resetn,
 input wire mem_valid,mem_ready,mem_sof,mem_eol,input wire [1:0] mem_keep,
 input wire [31:0] mem_awaddr,input wire [7:0] mem_awlen,input wire [2:0] mem_awsize,
 input wire [1:0] mem_awburst,input wire mem_awvalid,mem_awready,
 input wire [7:0] mem_wstrb,input wire mem_wvalid,mem_wready,
 input wire [1:0] mem_bresp,input wire mem_bvalid,mem_bready
);
 reg [63:0] cam_cycles=0,mem_cycles=0,axi_cycles=0;
 reg [31:0] vsync_count=0,done_count=0,drop_count=0,overflow_count=0;
 reg [31:0] cam_epoch=0,mem_epoch=0,last_vsync=0,last_done=0;
 reg [31:0] vsync_period=0,done_period=0;
 reg cam_reset_d=0,mem_reset_d=0,vsync_seen=0,done_seen=0;
 reg vsync_period_valid=0,done_period_valid=0;
 wire mem_reset=!mem_resetn;
 always @(posedge cam_clk) begin
  cam_cycles<=cam_cycles+1'b1;cam_reset_d<=cam_reset;
  if(cam_reset&&!cam_reset_d) cam_epoch<=cam_epoch+1'b1;
  if(cam_reset) begin vsync_seen<=0;done_seen<=0;vsync_period_valid<=0;done_period_valid<=0;end
  else begin
   if(cam_vsync) begin
    vsync_count<=vsync_count+1'b1;last_vsync<=cam_cycles[31:0];vsync_seen<=1;
    if(vsync_seen) begin vsync_period<=cam_cycles[31:0]-last_vsync;vsync_period_valid<=1;end
   end
   if(cam_done) begin
    done_count<=done_count+1'b1;last_done<=cam_cycles[31:0];done_seen<=1;
    if(done_seen) begin done_period<=cam_cycles[31:0]-last_done;done_period_valid<=1;end
   end
   drop_count<=drop_count+cam_drop_events;
   if(cam_overflow_event) overflow_count<=overflow_count+1'b1;
  end
 end
 always @(posedge mem_clk) begin
  mem_cycles<=mem_cycles+1'b1;mem_reset_d<=mem_reset;
  if(mem_reset&&!mem_reset_d) mem_epoch<=mem_epoch+1'b1;
 end
 wire [31:0] cb,cs,ce,cerr,cstall,mb,ms,me,merr,mstall;
 video_axis_monitor #(.WIDTH(WIDTH),.HEIGHT(HEIGHT)) cam_axis_monitor(
  .clk(cam_clk),.reset(cam_reset),.valid(cam_valid),.ready(cam_ready),
  .sof(cam_sof),.eol(cam_eol),.keep(cam_keep),
  .beats(cb),.sofs(cs),.eols(ce),.errors(cerr),.stalls(cstall));
 video_axis_monitor #(.WIDTH(WIDTH),.HEIGHT(HEIGHT)) mem_axis_monitor(
  .clk(mem_clk),.reset(mem_reset),.valid(mem_valid),.ready(mem_ready),
  .sof(mem_sof),.eol(mem_eol),.keep(mem_keep),
  .beats(mb),.sofs(ms),.eols(me),.errors(merr),.stalls(mstall));
 wire [31:0] aw_count,b_count,write_complete,write_errors,write_fault,pending;
 s2mm_write_observer #(.FRAME_BYTES(WIDTH*HEIGHT*2)) write_monitor(
  .clk(mem_clk),.reset(mem_reset),.awaddr(mem_awaddr),.awlen(mem_awlen),
  .awsize(mem_awsize),.awburst(mem_awburst),.awid(1'b0),.awvalid(mem_awvalid),.awready(mem_awready),
  .wstrb(mem_wstrb),.wvalid(mem_wvalid),.wready(mem_wready),
  .bid(1'b0),.bresp(mem_bresp),.bvalid(mem_bvalid),.bready(mem_bready),
  .aw_count(aw_count),.b_count(b_count),.complete_count(write_complete),
  .error_count(write_errors),.fault(write_fault),.pending(pending));
 wire [479:0] cam_live={29'd0,done_period_valid,vsync_period_valid,cam_reset,
  done_period,vsync_period,cam_epoch,cstall,cerr,ce,cs,cb,overflow_count,drop_count,done_count,vsync_count,cam_cycles};
 wire [447:0] mem_live={31'd0,mem_reset,mem_epoch,pending,write_fault,write_errors,
  write_complete,b_count,aw_count,merr,me,ms,mb,mem_cycles};
 wire [479:0] cam_payload;
 wire [447:0] mem_payload;
 wire cam_payload_valid,mem_payload_valid,cam_completed,mem_completed;
 reg toggle=0,busy=0,valid=0,timed_out=0,got_cam=0,got_mem=0;
 reg [31:0] seq=0,wait_cycles=0;
 reg [63:0] request_time=0,complete_time=0;
 reg [479:0] cam_snapshot=0;
 reg [447:0] mem_snapshot=0;
 stats_snapshot_cdc #(.WIDTH(480)) cam_snapshot_cdc(
  .source_clk(cam_clk),.destination_clk(axi_clk),.request_toggle(toggle),.live_data(cam_live),
  .snapshot_data(cam_payload),.snapshot_valid(cam_payload_valid),.completed_toggle(cam_completed));
 stats_snapshot_cdc #(.WIDTH(448)) mem_snapshot_cdc(
  .source_clk(mem_clk),.destination_clk(axi_clk),.request_toggle(toggle),.live_data(mem_live),
  .snapshot_data(mem_payload),.snapshot_valid(mem_payload_valid),.completed_toggle(mem_completed));
 assign request_ready=!busy && cam_completed==toggle && mem_completed==toggle;
 always @(posedge axi_clk) begin
  axi_cycles<=axi_cycles+1'b1;
  if(cam_payload_valid) begin cam_snapshot<=cam_payload;got_cam<=1;end
  if(mem_payload_valid) begin mem_snapshot<=mem_payload;got_mem<=1;end
  if(request && request_ready) begin
   toggle<=!toggle;busy<=1;valid<=0;timed_out<=0;got_cam<=0;got_mem<=0;
   request_time<=axi_cycles;wait_cycles<=0;
  end else if(busy) begin
   wait_cycles<=wait_cycles+1'b1;
   if(got_cam && got_mem && cam_completed==toggle && mem_completed==toggle) begin
    busy<=0;valid<=!cam_snapshot[448]&&!mem_snapshot[416];
    seq<=seq+1'b1;complete_time<=axi_cycles;
   end else if(wait_cycles==TIMEOUT_CYCLES-1) begin
    busy<=0;valid<=0;timed_out<=1;complete_time<=axi_cycles;
   end
  end
 end
 always @* begin
  read_data=0;
  case(read_addr)
   8'h40:read_data=32'h56333353;
   8'h44:read_data=32'd1;
   8'h4c:read_data={25'd0,(!busy&&!request_ready),timed_out,(mem_snapshot[351:320]!=0),
    mem_snapshot[416],cam_snapshot[448],valid,busy};
   8'h50:read_data=seq;
   8'h54:read_data=request_time[31:0];
   8'h58:read_data=request_time[63:32];
   8'h5c:read_data=complete_time[31:0];
   8'h60:read_data=complete_time[63:32];
   8'h64:read_data=cam_snapshot[31:0];
   8'h68:read_data=cam_snapshot[63:32];
   8'h6c:read_data=cam_snapshot[95:64];
   8'h70:read_data=cam_snapshot[127:96];
   8'h74:read_data=cam_snapshot[159:128];
   8'h78:read_data=cam_snapshot[191:160];
   8'h7c:read_data=cam_snapshot[223:192];
   8'h80:read_data=cam_snapshot[255:224];
   8'h84:read_data=cam_snapshot[287:256];
   8'h88:read_data=cam_snapshot[319:288];
   8'h8c:read_data=cam_snapshot[351:320];
   8'h90:read_data=mem_snapshot[31:0];
   8'h94:read_data=mem_snapshot[63:32];
   8'h98:read_data=mem_snapshot[95:64];
   8'h9c:read_data=mem_snapshot[127:96];
   8'ha0:read_data=mem_snapshot[159:128];
   8'ha4:read_data=mem_snapshot[191:160];
   8'ha8:read_data=mem_snapshot[223:192];
   8'hac:read_data=mem_snapshot[255:224];
   8'hb0:read_data=mem_snapshot[287:256];
   8'hb4:read_data=mem_snapshot[319:288];
   8'hb8:read_data=mem_snapshot[351:320];
   8'hbc:read_data=mem_snapshot[383:352];
   8'hc0:read_data=cam_snapshot[383:352];
   8'hc4:read_data=mem_snapshot[415:384];
   8'hc8:read_data=cam_snapshot[415:384];
   8'hcc:read_data=cam_snapshot[447:416];
   8'hd0:read_data={30'd0,cam_snapshot[450:449]};
   default:read_data=0;
  endcase
 end
endmodule
