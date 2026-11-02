`timescale 1ns/1ps
// The payload is frozen in its source domain until the XPM handshake finishes.
// reset of the measured datapath deliberately does not reset this transport.
module stats_snapshot_cdc #(parameter integer WIDTH=32)(
 input wire source_clk, input wire destination_clk,
 input wire request_toggle, input wire [WIDTH-1:0] live_data,
 output wire [WIDTH-1:0] snapshot_data, output wire snapshot_valid,
 output wire completed_toggle
);
 wire request_source, received;
 reg [WIDTH-1:0] frozen_data=0;
 reg send=0, seen=0;
 reg [1:0] state=0;
 xpm_cdc_single #(.DEST_SYNC_FF(3),.INIT_SYNC_FF(1),.SRC_INPUT_REG(0)) request_cdc(
  .src_clk(destination_clk),.src_in(request_toggle),.dest_clk(source_clk),.dest_out(request_source));
 always @(posedge source_clk) begin
  case(state)
   0: if(request_source!=seen) begin frozen_data<=live_data;send<=1;state<=1;end
   1: if(received) begin send<=0;state<=2;end
   2: if(!received) begin seen<=request_source;state<=0;end
   default: state<=0;
  endcase
 end
 xpm_cdc_handshake #(.DEST_EXT_HSK(0),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),
  .INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1),.WIDTH(WIDTH)) payload_cdc(
  .src_clk(source_clk),.src_in(frozen_data),.src_send(send),.src_rcv(received),
  .dest_clk(destination_clk),.dest_out(snapshot_data),.dest_req(snapshot_valid),.dest_ack(1'b0));
 xpm_cdc_single #(.DEST_SYNC_FF(3),.INIT_SYNC_FF(1),.SRC_INPUT_REG(0)) completed_cdc(
  .src_clk(source_clk),.src_in(seen),.dest_clk(destination_clk),.dest_out(completed_toggle));
endmodule
