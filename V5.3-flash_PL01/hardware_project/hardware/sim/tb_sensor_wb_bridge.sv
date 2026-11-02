`timescale 1ns/1ps
module tb_sensor_wb_bridge;
 reg axi_clk=0,clk50=0,run50=1;
 always #5 axi_clk=~axi_clk;
 always #10 if(run50)clk50=~clk50;
 reg [127:0] command=0,response50=0;reg command_send=0,request_ack50=0,response_send50=0;
 reg abort=0;wire abort50;
 wire command_received,result_valid,request_valid50,response_received50;
 wire [127:0] result,request50;
 sensor_wb_bridge dut(.*);
 integer result_count=0,checks=0;reg [127:0] observed_result=0;
 always @(posedge axi_clk)if(result_valid)begin observed_result<=result;result_count<=result_count+1;end
 task check(input bit condition,input string message);
  begin if(!condition)$fatal(1,"CHECK_FAILED: %s",message);checks=checks+1;end
 endtask
 task transfer(input [127:0] value,input [127:0] answer);
  integer before_count;begin
   before_count=result_count;
   @(negedge axi_clk);command=value;command_send=1;
   wait(request_valid50);#1;check(request50===value,"all 128 request bits arrive together");
   repeat(5)begin @(posedge clk50);#1;check(request_valid50&&request50===value,"request held stable before external ack");end
   check(!command_received,"external acknowledgement required");
   @(negedge clk50);request_ack50=1;
   wait(command_received);@(negedge axi_clk);command_send=0;
   wait(!request_valid50);@(negedge clk50);request_ack50=0;
   wait(!command_received);
   @(negedge clk50);response50=answer;response_send50=1;
   wait(response_received50);@(negedge clk50);response_send50=0;
   wait(!response_received50);repeat(5)@(negedge axi_clk);
   check(result_count==before_count+1,"one result-valid event per response");
   check(observed_result===answer,"all 128 result bits arrive together");
  end
 endtask
 initial begin
  #250;
  transfer(128'h12345678_9abcdef0_01234567_89abcdef,128'h12345678_00000403_fedcba98_76543210);
  transfer(128'hffffffff_00000000_aaaaaaaa_55555555,128'hffffffff_00000005_00000000_00000000);
  @(negedge clk50);run50=0;
  @(negedge axi_clk);command=128'h00000003_12345678_55555555_00300a01;command_send=1;
  repeat(30)@(negedge axi_clk);check(!command_received,"stopped sensor clock cannot acknowledge");
  run50=1;wait(request_valid50);#1;check(request50===command,"bundle survives stalled destination clock");
  @(negedge clk50);request_ack50=1;wait(command_received);@(negedge axi_clk);command_send=0;
  wait(!request_valid50);@(negedge clk50);request_ack50=0;wait(!command_received);
  $display("PASS_SENSOR_WB_BRIDGE checks=%0d",checks);$finish;
 end
 initial begin #200000;$fatal(1,"TB watchdog");end
endmodule
