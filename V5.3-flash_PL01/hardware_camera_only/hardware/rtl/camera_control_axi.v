`timescale 1ns/1ps
// One outstanding operation. AW and W may arrive independently, in any order.
module camera_control_axi_v33 #(
 parameter [31:0] WB_TIMEOUT_CYCLES=32'd3500000000
)(
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME s_axi_aclk, ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET s_axi_aresetn, FREQ_HZ 100000000" *)
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *) input wire s_axi_aclk,
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME s_axi_aresetn, POLARITY ACTIVE_LOW" *)
 (* X_INTERFACE_INFO="xilinx.com:signal:reset:1.0 s_axi_aresetn RST" *) input wire s_axi_aresetn,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *) input wire [7:0] s_axi_awaddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWPROT" *) input wire [2:0] s_axi_awprot,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *) input wire s_axi_awvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *) output wire s_axi_awready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WDATA" *) input wire [31:0] s_axi_wdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *) input wire [3:0] s_axi_wstrb,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WVALID" *) input wire s_axi_wvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI WREADY" *) output wire s_axi_wready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BRESP" *) output reg [1:0] s_axi_bresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BVALID" *) output reg s_axi_bvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI BREADY" *) input wire s_axi_bready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *) input wire [7:0] s_axi_araddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARPROT" *) input wire [2:0] s_axi_arprot,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *) input wire s_axi_arvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *) output wire s_axi_arready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RDATA" *) output reg [31:0] s_axi_rdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RRESP" *) output reg [1:0] s_axi_rresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RVALID" *) output reg s_axi_rvalid,
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 8, HAS_BURST 0, HAS_LOCK 0, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, SUPPORTS_NARROW_BURST 0, NUM_READ_OUTSTANDING 1, NUM_WRITE_OUTSTANDING 1" *)
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 S_AXI RREADY" *) input wire s_axi_rready,
 output reg [3:0] command, output reg command_send, input wire command_received,
 input wire [31:0] result,input wire result_valid,
 output reg [127:0] sensor_command,output reg sensor_command_send,input wire sensor_command_received,
 input wire [127:0] sensor_result,input wire sensor_result_valid,
 output wire sensor_abort,
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 stats_cam_clk CLK" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME stats_cam_clk, ASSOCIATED_BUSIF CAM_MON, FREQ_HZ 83000000" *) input wire stats_cam_clk,
 input wire stats_cam_reset,stats_cam_vsync,stats_cam_done,
 input wire [1:0] stats_cam_drop_events,input wire stats_cam_overflow_event,
 (* X_INTERFACE_MODE="monitor" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME CAM_MON, TDATA_NUM_BYTES 2, HAS_TKEEP 1, HAS_TLAST 1, TUSER_WIDTH 1" *)
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TVALID" *) input wire stats_cam_valid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TREADY" *) input wire stats_cam_ready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TUSER" *) input wire stats_cam_sof,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TLAST" *) input wire stats_cam_eol,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TKEEP" *) input wire [1:0] stats_cam_keep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 CAM_MON TDATA" *) input wire [15:0] stats_cam_data,
 (* X_INTERFACE_INFO="xilinx.com:signal:clock:1.0 stats_mem_clk CLK" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME stats_mem_clk, ASSOCIATED_BUSIF MEM_MON:WRITE_MON, FREQ_HZ 142857132" *) input wire stats_mem_clk,
 input wire stats_mem_resetn,
 (* X_INTERFACE_MODE="monitor" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME MEM_MON, TDATA_NUM_BYTES 2, HAS_TKEEP 1, HAS_TLAST 1, TUSER_WIDTH 1" *)
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TVALID" *) input wire stats_mem_valid,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TREADY" *) input wire stats_mem_ready,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TUSER" *) input wire stats_mem_sof,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TLAST" *) input wire stats_mem_eol,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TKEEP" *) input wire [1:0] stats_mem_keep,
 (* X_INTERFACE_INFO="xilinx.com:interface:axis:1.0 MEM_MON TDATA" *) input wire [15:0] stats_mem_data,
 (* X_INTERFACE_MODE="monitor" *)
 (* X_INTERFACE_PARAMETER="XIL_INTERFACENAME WRITE_MON, PROTOCOL AXI4, READ_WRITE_MODE WRITE_ONLY, DATA_WIDTH 64, ADDR_WIDTH 32, ID_WIDTH 0" *)
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWADDR" *) input wire [31:0] stats_mem_awaddr,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWLEN" *) input wire [7:0] stats_mem_awlen,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWSIZE" *) input wire [2:0] stats_mem_awsize,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWBURST" *) input wire [1:0] stats_mem_awburst,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWVALID" *) input wire stats_mem_awvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON AWREADY" *) input wire stats_mem_awready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON WDATA" *) input wire [63:0] stats_mem_wdata,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON WLAST" *) input wire stats_mem_wlast,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON WSTRB" *) input wire [7:0] stats_mem_wstrb,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON WVALID" *) input wire stats_mem_wvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON WREADY" *) input wire stats_mem_wready,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON BRESP" *) input wire [1:0] stats_mem_bresp,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON BVALID" *) input wire stats_mem_bvalid,
 (* X_INTERFACE_INFO="xilinx.com:interface:aximm:1.0 WRITE_MON BREADY" *) input wire stats_mem_bready
);
 reg stats_request=0;
 wire stats_ready;
 wire [31:0] stats_data;
 video_stats_snapshot stats_i(
  .axi_clk(s_axi_aclk),.request(stats_request),.read_addr(s_axi_araddr),.read_data(stats_data),.request_ready(stats_ready),
  .cam_clk(stats_cam_clk),.cam_reset(stats_cam_reset),.cam_vsync(stats_cam_vsync),.cam_done(stats_cam_done),
  .cam_drop_events(stats_cam_drop_events),.cam_overflow_event(stats_cam_overflow_event),
  .cam_valid(stats_cam_valid),.cam_ready(stats_cam_ready),.cam_sof(stats_cam_sof),.cam_eol(stats_cam_eol),.cam_keep(stats_cam_keep),
  .mem_clk(stats_mem_clk),.mem_resetn(stats_mem_resetn),
  .mem_valid(stats_mem_valid),.mem_ready(stats_mem_ready),.mem_sof(stats_mem_sof),.mem_eol(stats_mem_eol),.mem_keep(stats_mem_keep),
  .mem_awaddr(stats_mem_awaddr),.mem_awlen(stats_mem_awlen),.mem_awsize(stats_mem_awsize),.mem_awburst(stats_mem_awburst),
  .mem_awvalid(stats_mem_awvalid),.mem_awready(stats_mem_awready),
  .mem_wstrb(stats_mem_wstrb),.mem_wvalid(stats_mem_wvalid),.mem_wready(stats_mem_wready),
  .mem_bresp(stats_mem_bresp),.mem_bvalid(stats_mem_bvalid),.mem_bready(stats_mem_bready));
 reg [3:0] control;
 reg operation_busy,completion_valid;
 reg [31:0] active,sequence_count;
 reg aw_pending,w_pending;
 reg [7:0] aw_saved;
 reg [31:0] w_saved;
 reg [3:0] strobes;
 reg [31:0] wb_req0,wb_req1,wb_req2;
 reg wb_busy,wb_valid,wb_armed,wb_fatal;
 reg [31:0] wb_sequence,wb_completed,wb_flags,wb_timer;
 reg [63:0] wb_data;
 // Fatal is sticky across AXI reset and must reach the Sensor sequencer even
 // if its clock was stopped when the endpoint transport deadline expired.
 assign sensor_abort=wb_fatal;
 wire wb_ready=!wb_busy && !wb_fatal && !sensor_command_send && !sensor_command_received &&
               !operation_busy && !command_send && !command_received;
 wire wb_mutating=(wb_req0[7:0]==8'h10 || wb_req0[7:0]==8'h11);
 // The clk50 sequencer independently enforces the Sensor address/mask policy.
 // The endpoint validates the ABI shape and never forwards an unarmed mutation.
 reg wb_request_shape_valid;
 always @* begin
  wb_request_shape_valid=0;
  case(wb_req0[7:0])
   8'h01:wb_request_shape_valid=(wb_req0[31:24]==0 && wb_req1==0 && wb_req2==0);
   8'h02,8'h03:wb_request_shape_valid=(wb_req0[31:8]==0 && wb_req1==0 && wb_req2==0);
   8'h10:wb_request_shape_valid=(wb_req0[31:24]!=0 && wb_req1[31:8]==0 && wb_req2==0);
   8'h11:wb_request_shape_valid=(wb_req0[31:8]==0 && wb_req1[7:0]==0 && wb_req2[31:12]==0 &&
                                wb_req1[19:8]>=12'h100 && wb_req1[31:20]>=12'h100 && wb_req2[11:0]>=12'h100);
   default:wb_request_shape_valid=0;
  endcase
 end
 wire wb_result_data_valid=sensor_result[65] && sensor_result[74] && sensor_result[73:66]==0;
 // Transaction ownership survives an AXI bus reset. Only loading a new PL
 // bitstream initializes the operation lifetime; resetting video is forbidden.
 initial begin
  command=0;command_send=0;operation_busy=0;completion_valid=0;active=0;sequence_count=0;
  sensor_command=0;sensor_command_send=0;wb_busy=0;wb_valid=0;wb_armed=0;wb_fatal=0;
  wb_sequence=0;wb_completed=0;wb_flags=0;wb_data=0;wb_timer=0;
 end
 assign s_axi_awready=s_axi_aresetn && !aw_pending && !s_axi_bvalid;
 assign s_axi_wready=s_axi_aresetn && !w_pending && !s_axi_bvalid;
 assign s_axi_arready=s_axi_aresetn && !s_axi_rvalid;
 always @(posedge s_axi_aclk) begin
  stats_request<=0;
  if(command_received)command_send<=0;
  if(sensor_command_received)sensor_command_send<=0;
  if(result_valid && operation_busy)begin
   active<=result;operation_busy<=0;completion_valid<=1;sequence_count<=sequence_count+1'b1;
  end
  if(!s_axi_aresetn) begin
   control<=4'h2;aw_pending<=0;w_pending<=0;aw_saved<=0;w_saved<=0;strobes<=0;
   wb_req0<=0;wb_req1<=0;wb_req2<=0;wb_armed<=0;
   s_axi_bvalid<=0;s_axi_bresp<=0;s_axi_rvalid<=0;s_axi_rresp<=0;s_axi_rdata<=0;
  end else begin
   if(s_axi_awvalid && s_axi_awready)begin aw_saved<=s_axi_awaddr;aw_pending<=1;end
   if(s_axi_wvalid && s_axi_wready)begin w_saved<=s_axi_wdata;strobes<=s_axi_wstrb;w_pending<=1;end
   if(s_axi_bvalid && s_axi_bready)s_axi_bvalid<=0;
   if(aw_pending && w_pending && !s_axi_bvalid)begin
    aw_pending<=0;w_pending<=0;s_axi_bvalid<=1;s_axi_bresp<=0;
    case(aw_saved)
     8'h00:if(strobes[0])control<=w_saved[3:0];
     8'h04:if(strobes[0] && w_saved[0])begin
      if(operation_busy || command_send || command_received || wb_busy || wb_fatal ||
         sensor_command_send || sensor_command_received)s_axi_bresp<=2'b10;
      else begin command<=control;command_send<=1;operation_busy<=1;completion_valid<=0;end
     end
     8'h20,8'h24,8'h28:begin
      if(!wb_ready)s_axi_bresp<=2'b10;
      else begin
       if(aw_saved==8'h20)begin
        if(strobes[0])wb_req0[7:0]<=w_saved[7:0];if(strobes[1])wb_req0[15:8]<=w_saved[15:8];
        if(strobes[2])wb_req0[23:16]<=w_saved[23:16];if(strobes[3])wb_req0[31:24]<=w_saved[31:24];
       end else if(aw_saved==8'h24)begin
        if(strobes[0])wb_req1[7:0]<=w_saved[7:0];if(strobes[1])wb_req1[15:8]<=w_saved[15:8];
        if(strobes[2])wb_req1[23:16]<=w_saved[23:16];if(strobes[3])wb_req1[31:24]<=w_saved[31:24];
       end else begin
        if(strobes[0])wb_req2[7:0]<=w_saved[7:0];if(strobes[1])wb_req2[15:8]<=w_saved[15:8];
        if(strobes[2])wb_req2[23:16]<=w_saved[23:16];if(strobes[3])wb_req2[31:24]<=w_saved[31:24];
       end
      end
     end
     8'h2c:begin
      // Accepted reads also consume authorization; rejected mutations lock too.
      if(wb_mutating)wb_armed<=0;
      if(strobes!=4'hf || w_saved!=32'h53434342 || !wb_ready || !wb_request_shape_valid ||
         (wb_mutating && !wb_armed))begin
       s_axi_bresp<=2'b10;
       if(!wb_fatal)begin wb_valid<=0;wb_flags<=32'h00000021;wb_data<=0;end
      end else begin
       sensor_command<={wb_sequence+32'd1,wb_req2,wb_req1,wb_req0};sensor_command_send<=1;
       wb_sequence<=wb_sequence+1'b1;wb_busy<=1;wb_valid<=0;wb_armed<=0;
       wb_flags<=0;wb_data<=0;wb_timer<=0;
      end
     end
     8'h38:begin
      if(strobes==4'hf && w_saved==0)wb_armed<=0;
      else if(strobes==4'hf && w_saved==32'h57424731 && wb_ready)wb_armed<=1;
      else begin wb_armed<=0;s_axi_bresp<=2'b10;end
     end
     8'h48:if(strobes[0] && w_saved[0])begin
      if(!stats_ready || stats_request)s_axi_bresp<=2'b10;
      else stats_request<=1;
     end
     default:s_axi_bresp<=2'b10;
    endcase
   end
   if(s_axi_rvalid && s_axi_rready)s_axi_rvalid<=0;
   if(s_axi_arvalid && s_axi_arready)begin
    s_axi_rvalid<=1;s_axi_rresp<=0;
    case(s_axi_araddr)
     8'h00:s_axi_rdata<={28'd0,control};
     8'h04:s_axi_rdata<=0;
     8'h08:s_axi_rdata<={26'd0,active[7],active[4],active[6],active[5],completion_valid,operation_busy};
     8'h0c:s_axi_rdata<=active;
     8'h10:s_axi_rdata<=32'h56323543;
     8'h14:s_axi_rdata<=sequence_count;
     8'h18:s_axi_rdata<=32'h57423431;
     8'h1c:s_axi_rdata<={27'd0,wb_fatal,wb_ready,wb_armed,wb_valid,wb_busy};
     8'h20:s_axi_rdata<=wb_req0;
     8'h24:s_axi_rdata<=wb_req1;
     8'h28:s_axi_rdata<=wb_req2;
     8'h2c:s_axi_rdata<=0;
     8'h30:s_axi_rdata<=wb_sequence;
     8'h34:s_axi_rdata<=wb_completed;
     8'h38:s_axi_rdata<={31'd0,wb_armed};
     8'h3c:s_axi_rdata<=wb_flags;
     8'hd4:s_axi_rdata<=wb_data[31:0];
     8'hd8:s_axi_rdata<=wb_data[63:32];
     default:begin
      if(s_axi_araddr>=8'h40 && s_axi_araddr<=8'hd0 && s_axi_araddr[1:0]==0) s_axi_rdata<=stats_data;
      else begin s_axi_rdata<=0;s_axi_rresp<=2'b10;end
     end
    endcase
   end
  end
  // Transaction lifetime is outside AXI reset and wins over a simultaneous
  // rejected write. Otherwise that rejection could erase the one valid reply.
  // Timeout wins even if success arrives on the terminal clock edge.
  if(wb_busy && !wb_fatal)begin
   if(wb_timer>=WB_TIMEOUT_CYCLES-1'b1)begin
    wb_busy<=0;wb_valid<=1;wb_fatal<=1;wb_armed<=0;
    wb_completed<=wb_sequence;wb_flags<=32'h00000209;wb_data<=0;
   end else if(sensor_result_valid && sensor_result[127:96]==wb_sequence && sensor_result[64])begin
    wb_busy<=0;wb_valid<=1;wb_completed<=wb_sequence;
    wb_flags<=sensor_result[95:64] & (wb_result_data_valid?32'hffffffff:32'hfffffbff);
    wb_data<=wb_result_data_valid?sensor_result[63:0]:64'd0;
    if(sensor_result[73])begin wb_fatal<=1;wb_armed<=0;end
   end else wb_timer<=wb_timer+1'b1;
  end
 end
endmodule
