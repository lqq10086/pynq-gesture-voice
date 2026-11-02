`timescale 1ns/1ps
module tb_camera_control_wb;
 reg clk=0,camclk=0,memclk=0,rstn=0;
 always #5 clk=~clk;
 always #6 camclk=~camclk;
 always #3.5 memclk=~memclk;
 reg [7:0] awaddr=0,araddr=0;
 reg awvalid=0,wvalid=0,bready=0,arvalid=0,rready=0;
 reg [31:0] wdata=0;reg [3:0] wstrb=0;
 wire awready,wready,bvalid,arready,rvalid;wire [1:0] bresp,rresp;wire [31:0] rdata;
 wire [3:0] command;wire command_send;reg command_received=0;
 reg [31:0] result=0;reg result_valid=0;
 wire [127:0] sensor_command;wire sensor_command_send;reg sensor_command_received=0;
 reg [127:0] sensor_result=0;reg sensor_result_valid=0;
 camera_control_axi_v33
`ifdef WB_IMPLEMENTED
 #(.WB_TIMEOUT_CYCLES(32'd2048))
`endif
 dut(.s_axi_aclk(clk),.s_axi_aresetn(rstn),
 .s_axi_awaddr(awaddr),.s_axi_awprot(3'd0),.s_axi_awvalid(awvalid),.s_axi_awready(awready),
 .s_axi_wdata(wdata),.s_axi_wstrb(wstrb),.s_axi_wvalid(wvalid),.s_axi_wready(wready),
 .s_axi_bresp(bresp),.s_axi_bvalid(bvalid),.s_axi_bready(bready),
 .s_axi_araddr(araddr),.s_axi_arprot(3'd0),.s_axi_arvalid(arvalid),.s_axi_arready(arready),
 .s_axi_rdata(rdata),.s_axi_rresp(rresp),.s_axi_rvalid(rvalid),.s_axi_rready(rready),
 .command(command),.command_send(command_send),.command_received(command_received),.result(result),.result_valid(result_valid),
`ifdef WB_IMPLEMENTED
 .sensor_command(sensor_command),.sensor_command_send(sensor_command_send),.sensor_command_received(sensor_command_received),
 .sensor_result(sensor_result),.sensor_result_valid(sensor_result_valid),
`endif
 .stats_cam_clk(camclk),.stats_cam_reset(1'b0),.stats_cam_vsync(1'b0),.stats_cam_done(1'b0),
 .stats_cam_drop_events(2'd0),.stats_cam_overflow_event(1'b0),.stats_cam_valid(1'b0),.stats_cam_ready(1'b1),
 .stats_cam_sof(1'b0),.stats_cam_eol(1'b0),.stats_cam_keep(2'b11),.stats_cam_data(16'd0),
 .stats_mem_clk(memclk),.stats_mem_resetn(1'b1),.stats_mem_valid(1'b0),.stats_mem_ready(1'b1),
 .stats_mem_sof(1'b0),.stats_mem_eol(1'b0),.stats_mem_keep(2'b11),.stats_mem_data(16'd0),
 .stats_mem_awaddr(32'd0),.stats_mem_awlen(8'd0),.stats_mem_awsize(3'd3),.stats_mem_awburst(2'd1),
 .stats_mem_awvalid(1'b0),.stats_mem_awready(1'b1),.stats_mem_wdata(64'd0),.stats_mem_wlast(1'b0),
 .stats_mem_wstrb(8'hff),.stats_mem_wvalid(1'b0),.stats_mem_wready(1'b1),.stats_mem_bresp(2'd0),
 .stats_mem_bvalid(1'b0),.stats_mem_bready(1'b1));
 integer checks=0;
 task check(input bit condition,input string message);
  begin if(!condition)$fatal(1,"CHECK_FAILED: %s",message);checks=checks+1;end
 endtask
 task aw(input [7:0] a);
  begin @(negedge clk);awaddr=a;awvalid=1;do @(posedge clk);while(!awready);@(negedge clk);awvalid=0;end
 endtask
 task wd(input [31:0] d,input [3:0] s);
  begin @(negedge clk);wdata=d;wstrb=s;wvalid=1;do @(posedge clk);while(!wready);@(negedge clk);wvalid=0;end
 endtask
 task write_bus(input [7:0] a,input [31:0] d,input [3:0] s,input integer order,input [1:0] expected);
  reg [1:0] held;begin
   if(order==0)begin aw(a);repeat(3)@(negedge clk);wd(d,s);end
   else if(order==1)begin wd(d,s);repeat(3)@(negedge clk);aw(a);end
   else fork aw(a);wd(d,s);join
   wait(bvalid);#1;held=bresp;check(bresp===expected,$sformatf("write %h response %h expected %h",a,bresp,expected));
   repeat(3)begin @(posedge clk);#1;check(bvalid&&bresp===held,"B response survives backpressure");end
   @(negedge clk);bready=1;@(negedge clk);bready=0;
  end
 endtask
 task read_bus(input [7:0] a,input [31:0] expected,input [31:0] mask,input [1:0] response);
  reg [31:0] held;begin
   @(negedge clk);araddr=a;arvalid=1;do @(posedge clk);while(!arready);@(negedge clk);arvalid=0;
   wait(rvalid);#1;held=rdata;
   check(rresp===response,$sformatf("read %h response %h expected %h",a,rresp,response));
   check((rdata&mask)===(expected&mask),$sformatf("read %h data %h expected %h mask %h",a,rdata,expected,mask));
   repeat(3)begin @(posedge clk);#1;check(rvalid&&rdata===held,"R response survives backpressure");end
   @(negedge clk);rready=1;@(negedge clk);rready=0;
  end
 endtask
 task reply(input [31:0] seq,input [31:0] flags,input [63:0] data);
  begin @(negedge clk);sensor_result={seq,flags,data};sensor_result_valid=1;@(negedge clk);sensor_result_valid=0;end
 endtask
 task ack_sensor;
  begin @(negedge clk);sensor_command_received=1;repeat(2)@(negedge clk);sensor_command_received=0;end
 endtask
 task stage_read;
  begin write_bus(8'h20,32'h00300a01,4'hf,0,0);write_bus(8'h24,0,4'hf,1,0);write_bus(8'h28,0,4'hf,2,0);end
 endtask
 initial begin
  #200;@(negedge clk);rstn=1;
  read_bus(8'h10,32'h56323543,32'hffffffff,0);
  read_bus(8'h00,2,32'hffffffff,0);
  read_bus(8'h40,32'h56333353,32'hffffffff,0);
  read_bus(8'h18,32'h57423431,32'hffffffff,0); // Baseline must fail here: WB ABI absent.
`ifdef WB_IMPLEMENTED
  read_bus(8'h1c,8,32'h1f,0);read_bus(8'h30,0,32'hffffffff,0);
  // Reset discards unmatched AXI channel halves, not transaction ownership.
  aw(8'h20);@(negedge clk);rstn=0;repeat(3)@(negedge clk);rstn=1;
  wd(32'hdeadbeef,4'hf);repeat(4)@(negedge clk);check(!bvalid,"reset discarded pending AW");
  rstn=0;repeat(3)@(negedge clk);rstn=1;
  aw(8'h20);repeat(4)@(negedge clk);check(!bvalid,"reset discarded pending W");
  rstn=0;repeat(3)@(negedge clk);rstn=1;
  write_bus(8'h24,32'h11223344,4'hf,0,0);write_bus(8'h24,32'haabbccdd,4'b0101,1,0);
  read_bus(8'h24,32'h11bb33dd,32'hffffffff,0);
  write_bus(8'h2c,32'h53434342,4'h7,2,2);read_bus(8'h30,0,32'hffffffff,0);
  write_bus(8'h20,32'h000000ff,4'hf,2,0);write_bus(8'h2c,32'h53434342,4'hf,1,2);
  read_bus(8'h30,0,32'hffffffff,0);read_bus(8'hdc,0,32'hffffffff,2);
  stage_read();write_bus(8'h2c,32'h53434342,4'hf,2,0);
  check(sensor_command==={32'd1,32'd0,32'd0,32'h00300a01},"request bundle and sequence 1");
  check(sensor_command_send,"request stays high before received");
  write_bus(8'h04,1,4'hf,1,2); // legacy cannot overlap WB
  write_bus(8'h20,32'h00300b01,4'hf,0,2);
  check(sensor_command[31:0]===32'h00300a01,"in-flight payload immutable");
  write_bus(8'h2c,32'h53434342,4'hf,2,2);read_bus(8'h30,1,32'hffffffff,0);
  ack_sensor();reply(32'd99,32'h403,64'hbad);read_bus(8'h1c,1,1,0);
  // A rejected second submit on the exact completion cycle must not erase
  // the legitimate in-flight response or give it the rejected request's identity.
  fork
   write_bus(8'h2c,32'h53434342,4'hf,2,2);
   begin
    wait(dut.aw_pending && dut.w_pending && !bvalid);
    reply(1,32'h403,64'h0123456789abcdef);
   end
  join
  read_bus(8'h1c,2,2,0);read_bus(8'h34,1,32'hffffffff,0);
  read_bus(8'h3c,32'h403,32'hffffffff,0);read_bus(8'hd4,32'h89abcdef,32'hffffffff,0);
  read_bus(8'hd8,32'h01234567,32'hffffffff,0);
  write_bus(8'h2c,0,4'hf,0,2);read_bus(8'h1c,0,2,0);read_bus(8'hd4,0,32'hffffffff,0);
  read_bus(8'h3c,32'h21,32'hffffffff,0); // rejection replaces stale SUCCESS
  write_bus(8'h20,32'h01340610,4'hf,1,0);write_bus(8'h24,1,4'hf,2,0);
  write_bus(8'h2c,32'h53434342,4'hf,0,2);read_bus(8'h30,1,32'hffffffff,0);
  write_bus(8'h38,32'h57424731,4'h7,2,2);read_bus(8'h1c,0,4,0);
  write_bus(8'h38,32'h57424731,4'hf,0,0);read_bus(8'h1c,4,4,0);
  stage_read();write_bus(8'h2c,32'h53434342,4'hf,2,0); // accepted read also consumes arm
  read_bus(8'h1c,0,4,0);ack_sensor();reply(2,32'h405,64'hffffffffffffffff);
  read_bus(8'h3c,5,32'hffffffff,0);read_bus(8'hd4,0,32'hffffffff,0); // DATA_VALID sanitized on failure
  write_bus(8'h20,32'h01340610,4'hf,1,0);write_bus(8'h24,1,4'hf,2,0);
  write_bus(8'h38,32'h57424731,4'hf,0,0);write_bus(8'h2c,32'h53434342,4'hf,2,0);
  read_bus(8'h1c,1,5,0);ack_sensor();reply(3,32'h403,64'h0000000000010100);
  write_bus(8'h2c,32'h53434342,4'hf,2,2);read_bus(8'h30,3,32'hffffffff,0);
  write_bus(8'h38,32'h57424731,4'hf,0,0);@(negedge clk);rstn=0;repeat(3)@(negedge clk);rstn=1;
  read_bus(8'h1c,0,4,0);read_bus(8'h30,3,32'hffffffff,0);
  stage_read();write_bus(8'h2c,32'h53434342,4'hf,2,0);
  @(negedge clk);rstn=0;repeat(3)@(negedge clk);
  check(sensor_command_send&&sensor_command[127:96]==4,"AXI reset preserves in-flight request and sequence");
  ack_sensor();reply(4,32'h403,64'h55);repeat(3)@(negedge clk);rstn=1;
  read_bus(8'h34,4,32'hffffffff,0);read_bus(8'hd4,32'h55,32'hffffffff,0);
  write_bus(8'h04,1,4'hf,0,0);check(command_send&&command==2,"legacy APPLY still works");
  write_bus(8'h2c,32'h53434342,4'hf,2,2);read_bus(8'h30,4,32'hffffffff,0);
  @(negedge clk);command_received=1;@(negedge clk);command_received=0;result=32'h9c;result_valid=1;
  @(negedge clk);result_valid=0;read_bus(8'h14,1,32'hffffffff,0);read_bus(8'h0c,32'h9c,32'hffffffff,0);
  write_bus(8'h48,1,4'hf,2,0);repeat(100)@(negedge clk);read_bus(8'h50,1,32'hffffffff,0);
  stage_read();write_bus(8'h2c,32'h53434342,4'hf,0,0); // request 5, no ACK: terminal timeout
  wait(dut.wb_timer==32'd2047);reply(5,32'h403,64'hcafe); // same terminal edge still times out
  repeat(10)@(negedge clk);
  read_bus(8'h1c,32'h12,32'h1f,0);read_bus(8'h34,5,32'hffffffff,0);read_bus(8'h3c,32'h209,32'hffffffff,0);
  check(sensor_command_send,"timeout preserves outstanding XPM send until acknowledgement");
  ack_sensor();reply(5,32'h403,64'h1234);read_bus(8'h3c,32'h209,32'hffffffff,0);read_bus(8'hd4,0,32'hffffffff,0);
  write_bus(8'h04,1,4'hf,2,2);write_bus(8'h38,32'h57424731,4'hf,2,2);
  @(negedge clk);rstn=0;repeat(3)@(negedge clk);rstn=1;read_bus(8'h1c,32'h12,32'h1f,0);
  $display("PASS_CAMERA_CONTROL_WB checks=%0d",checks);$finish;
`endif
 end
 initial begin #200000;$fatal(1,"TB watchdog");end
endmodule
