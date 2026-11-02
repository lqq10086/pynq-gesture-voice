`timescale 1ns/1ps
// Complete bundles cross with XPM handshakes, never independent bit synchronizers.
// The pixel request is deliberately one bit; original SOF logic applies both stages.
module camera_control_bridge #(parameter integer TIMEOUT_CYCLES=2000000000)(
 input wire axi_clk,clk50,rst50,
 input wire [3:0] command,input wire command_send,output wire command_received,
 output wire [31:0] result,output wire result_valid,
 input wire [1:0] switches,input wire boot_isp,config_done,config_error,
 input wire isp_active,isp_busy,isp_done,isp_error,isp_fatal,af_error,
 output reg isp_request,isp_apply,pl_request,input wire pl_active_async,
 output reg pl_commit_send,input wire pl_commit_received
);
 wire [3:0] command50;wire command_valid50;reg command_ack;
 reg [31:0] response50;reg response_send;wire response_received;
 xpm_cdc_handshake #(.WIDTH(4),.DEST_EXT_HSK(1),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),.INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) command_cdc_i(
  .src_clk(axi_clk),.src_in(command),.src_send(command_send),.src_rcv(command_received),
  .dest_clk(clk50),.dest_out(command50),.dest_req(command_valid50),.dest_ack(command_ack));
 xpm_cdc_handshake #(.WIDTH(32),.DEST_EXT_HSK(0),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),.INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) result_cdc_i(
  .src_clk(clk50),.src_in(response50),.src_send(response_send),.src_rcv(response_received),
  .dest_clk(axi_clk),.dest_out(result),.dest_req(result_valid),.dest_ack(1'b0));
 wire pl_active;
 xpm_cdc_single #(.DEST_SYNC_FF(3),.SRC_INPUT_REG(0),.INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) active_cdc_i(
  .src_clk(1'b0),.src_in(pl_active_async),.dest_clk(clk50),.dest_out(pl_active));
 localparam IDLE=0,INIT_WAIT=1,ISP_WAIT=2,PL_WAIT=3,REPLY=4,DRAIN=5;
 reg [2:0] state;
 reg source_active,enable_active;
 reg [3:0] pending_control;
 reg [1:0] target_mode;
 reg operation_error,control_fatal;
 reg [$clog2(TIMEOUT_CYCLES+1)-1:0] timer;
 always @(posedge clk50)begin
  if(rst50)begin
   state<=IDLE;source_active<=0;enable_active<=1;pending_control<=2;target_mode<=0;
   response50<=0;response_send<=0;isp_request<=0;isp_apply<=0;pl_request<=0;
   operation_error<=0;control_fatal<=0;timer<=0;command_ack<=0;pl_commit_send<=0;
  end else begin
   isp_apply<=0;
   if(pl_commit_received)pl_commit_send<=0;
   if(!command_valid50)command_ack<=0;
   if(state==INIT_WAIT || state==ISP_WAIT || state==PL_WAIT)timer<=timer+1'b1;
   case(state)
    IDLE:begin
     timer<=0;
     if(!source_active && !control_fatal)pl_request<=switches[0];
     if(command_valid50 && !command_ack)begin
      command_ack<=1;
      pending_control<=command50;
      target_mode<=command50[0]?command50[3:2]:{boot_isp,switches[0]};
      operation_error<=0;state<=INIT_WAIT;
     end
    end
    INIT_WAIT:begin
     if(isp_fatal || control_fatal || (config_error && !config_done))begin operation_error<=1;state<=REPLY;end
     else if(config_done && !isp_busy && !pl_commit_send && !pl_commit_received)begin
      if(target_mode[1]!=isp_active)begin isp_request<=target_mode[1];isp_apply<=1;state<=ISP_WAIT;end
      else begin
       pl_request<=pending_control[0]?(pending_control[1] && target_mode[0]):target_mode[0];
       pl_commit_send<=1;state<=PL_WAIT;
      end
     end
    end
    ISP_WAIT:if(isp_done)begin
     if(isp_error || isp_fatal)begin operation_error<=1;state<=REPLY;end
     else begin
      pl_request<=pending_control[0]?(pending_control[1] && target_mode[0]):target_mode[0];
      pl_commit_send<=1;state<=PL_WAIT;
     end
    end
    PL_WAIT:begin
     if(pl_commit_received)begin
      source_active<=pending_control[0];enable_active<=pending_control[0]?pending_control[1]:1'b1;
      state<=REPLY;
     end
    end
    REPLY:begin
     response50<={24'd0,af_error,(isp_fatal || control_fatal),operation_error,config_done,isp_active,pl_active,enable_active,source_active};
     response_send<=1;state<=DRAIN;
    end
    DRAIN:begin
     if(response_received)response_send<=0;
     if(!response_send && !response_received)state<=IDLE;
    end
    default:state<=IDLE;
   endcase
   if((state==INIT_WAIT || state==ISP_WAIT || state==PL_WAIT) && timer==TIMEOUT_CYCLES-1)begin
    // Keep outstanding XPM data/send stable until acknowledged. A stopped
    // video clock may return later, but this operation can never become success.
    operation_error<=1;control_fatal<=1;state<=REPLY;
   end
  end
 end
endmodule
