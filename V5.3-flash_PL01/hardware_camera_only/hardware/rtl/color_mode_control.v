`timescale 1ns/1ps
// J11 switches: debounce both; SW1 is latched once for the sensor boot profile.
module color_mode_control #(
 parameter integer DEBOUNCE_CYCLES=1000000
)(input wire clk,rst,sw0_async,sw1_async,
 output reg [1:0] switches,output reg boot_isp,boot_valid);
 localparam WIDTH=(DEBOUNCE_CYCLES<=1)?1:$clog2(DEBOUNCE_CYCLES);
 (* ASYNC_REG="TRUE" *) reg [1:0] sw_meta,sw_sync;
 reg [1:0] candidate;
 reg [WIDTH-1:0] count;
 always @(posedge clk) begin
  if(rst) begin
   sw_meta<=0;sw_sync<=0;candidate<=0;count<=0;switches<=0;boot_isp<=0;boot_valid<=0;
  end else begin
   sw_meta<={sw1_async,sw0_async};sw_sync<=sw_meta;
   if(sw_sync!=candidate) begin candidate<=sw_sync;count<=0;end
   else if(count==DEBOUNCE_CYCLES-1) begin
    switches<=candidate;
    if(!boot_valid) begin boot_isp<=candidate[1];boot_valid<=1;end
   end else count<=count+1'b1;
  end
 end
endmodule
