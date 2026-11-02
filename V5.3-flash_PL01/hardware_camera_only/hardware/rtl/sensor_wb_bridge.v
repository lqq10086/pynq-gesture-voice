`timescale 1ns/1ps
// Bundled transport only. Neither AXI reset nor video reset may interrupt an
// in-flight handshake. Endpoint/core ownership persists until PL is reloaded.
module sensor_wb_bridge(
 input wire axi_clk,clk50,
 input wire [127:0] command,input wire command_send,output wire command_received,
 output wire [127:0] result,output wire result_valid,
 input wire abort,output wire abort50,
 output wire [127:0] request50,output wire request_valid50,input wire request_ack50,
 input wire [127:0] response50,input wire response_send50,output wire response_received50
);
 xpm_cdc_handshake #(.WIDTH(128),.DEST_EXT_HSK(1),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),
  .INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) command_cdc_i(
  .src_clk(axi_clk),.src_in(command),.src_send(command_send),.src_rcv(command_received),
  .dest_clk(clk50),.dest_out(request50),.dest_req(request_valid50),.dest_ack(request_ack50));
 xpm_cdc_handshake #(.WIDTH(128),.DEST_EXT_HSK(0),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),
  .INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) result_cdc_i(
  .src_clk(clk50),.src_in(response50),.src_send(response_send50),.src_rcv(response_received50),
  .dest_clk(axi_clk),.dest_out(result),.dest_req(result_valid),.dest_ack(1'b0));
 // This is a persistent terminal level, not a pulse. Keeping it independent
 // of the request handshake also stops a previously accepted transaction.
 xpm_cdc_single #(.DEST_SYNC_FF(3),.SRC_INPUT_REG(1),.INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1)) abort_cdc_i(
  .src_clk(axi_clk),.src_in(abort),.dest_clk(clk50),.dest_out(abort50));
endmodule
