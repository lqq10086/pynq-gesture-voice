`timescale 1ns/1ps
module tb_wb_transport_abort #(parameter STOP_AFTER_ACCEPT=0);
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
 wire [127:0] sensor_command;wire sensor_command_send;wire sensor_command_received;
 wire [127:0] sensor_result;wire sensor_result_valid;
 wire sensor_abort,abort50;
`ifndef WB_ABORT_IMPLEMENTED
 assign sensor_abort=0;assign abort50=0;
`endif
 camera_control_axi_v33
`ifdef WB_IMPLEMENTED
 #(.WB_TIMEOUT_CYCLES(32'd64))
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
`ifdef WB_ABORT_IMPLEMENTED
 .sensor_abort(sensor_abort),
`endif
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
 task stage_read;
  begin write_bus(8'h20,32'h00300a01,4'hf,0,0);write_bus(8'h24,0,4'hf,1,0);write_bus(8'h28,0,4'hf,2,0);end
 endtask

 reg clk50=0,run50=1,resuming=0;
 always #10 if(run50)clk50=~clk50;
 wire [127:0] request50;wire request_valid50,response_received50;
 reg request_ack50=0,response_send50=0;reg [127:0] response50=0;
 integer seen_requests=0,resume_edges=0;
 sensor_wb_bridge bridge(.axi_clk(clk),.clk50(clk50),
  .command(sensor_command),.command_send(sensor_command_send),.command_received(sensor_command_received),
  .result(sensor_result),.result_valid(sensor_result_valid),
`ifdef WB_ABORT_IMPLEMENTED
  .abort(sensor_abort),.abort50(abort50),
`endif
  .request50(request50),.request_valid50(request_valid50),.request_ack50(request_ack50),
  .response50(response50),.response_send50(response_send50),.response_received50(response_received50));
 always @(posedge clk50)begin
  if(resuming)resume_edges<=resume_edges+1;
  if(!request_valid50)request_ack50<=0;
  if(request_valid50 && !request_ack50)begin
   if(!STOP_AFTER_ACCEPT)check(abort50,"queued request cannot reach sink before the prior terminal abort");
   check(request50[127:96]==1,"queued request retains original sequence");
   request_ack50<=1;seen_requests<=seen_requests+1;
  end
 end
 initial begin
  #200;@(negedge clk);rstn=1;stage_read();
  if(!STOP_AFTER_ACCEPT)begin @(negedge clk50);run50=0;end
  write_bus(8'h2c,32'h53434342,4'hf,2,0);
  if(STOP_AFTER_ACCEPT)begin
   wait(seen_requests==1);wait(!sensor_command_send && !sensor_command_received);
   @(negedge clk50);run50=0;
  end
  repeat(80)@(negedge clk);
  read_bus(8'h1c,32'h12,32'h1f,0);read_bus(8'h3c,32'h209,32'hffffffff,0);
  if(!STOP_AFTER_ACCEPT)check(sensor_command_send,"transport timeout keeps unacknowledged request stable");
  else check(!sensor_command_send,"accepted transaction timed out after request handshake drained");
  check(sensor_abort,"AXI terminal fatal must notify the stopped clk50 domain");
  check(!abort50,"stopped destination has not yet clocked the synchronous abort");
  resuming=1;run50=1;wait(abort50);#1;
  check(resume_edges<=4,"abort reaches resumed clk50 within synchronizer latency");
  wait(seen_requests==1);wait(!sensor_command_send && !sensor_command_received);
  repeat(8)begin @(negedge clk50);check(abort50,"terminal abort remains asserted");end
  @(negedge clk50);response50={32'd1,32'h403,64'h12345678};response_send50=1;
  wait(response_received50);@(negedge clk50);response_send50=0;wait(!response_received50);
  read_bus(8'h3c,32'h209,32'hffffffff,0);read_bus(8'hd4,0,32'hffffffff,0);
  @(negedge clk);rstn=0;repeat(5)@(negedge clk);rstn=1;
  check(sensor_abort&&abort50,"AXI reset cannot clear terminal abort in either domain");
  read_bus(8'h1c,32'h12,32'h1f,0);
  $display("PASS_WB_TRANSPORT_ABORT stop_after_accept=%0d checks=%0d",STOP_AFTER_ACCEPT,checks);$finish;
 end
 initial begin #200000;$fatal(1,"TB watchdog");end
endmodule
module tb_wb_transport_abort_after_accept;
 tb_wb_transport_abort #(.STOP_AFTER_ACCEPT(1)) scenario();
endmodule
