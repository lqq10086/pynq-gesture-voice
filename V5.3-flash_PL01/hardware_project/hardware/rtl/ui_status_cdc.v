`timescale 1ns/1ps
// Continuously publish a coherent pixel-domain status bundle to AXI GPIO.
// Payload is frozen until the XPM four-phase handshake completes.
module ui_status_cdc(
 input wire pix_clk,input wire ctrl_clk,
 input wire [127:0] live_data,output reg [127:0] status=0
);
 reg [127:0] frozen=0;
 reg [1:0] state=0;
 reg send=0;
 wire received,valid;
 wire [127:0] data;
 always @(posedge pix_clk) begin
  case(state)
   0: begin frozen<=live_data;send<=1;state<=1;end
   1: if(received) begin send<=0;state<=2;end
   2: if(!received) state<=0;
   default:state<=0;
  endcase
 end
 xpm_cdc_handshake #(.DEST_EXT_HSK(0),.DEST_SYNC_FF(3),.SRC_SYNC_FF(3),
  .INIT_SYNC_FF(1),.SIM_ASSERT_CHK(1),.WIDTH(128)) payload_cdc(
  .src_clk(pix_clk),.src_in(frozen),.src_send(send),.src_rcv(received),
  .dest_clk(ctrl_clk),.dest_out(data),.dest_req(valid),.dest_ack(1'b0));
 always @(posedge ctrl_clk) if(valid) status<=data;
endmodule
