`timescale 1ns/1ps
// Real camera_config, AF, WB sequencer and SCCB wire engine. The only external
// model is the camera's open-drain serial slave and its byte-addressed registers.
module tb_camera_wb_integration;
  reg clk50=0; always #10 clk50=~clk50;
  reg rst=1,frame_boundary=0,boot_isp=0,isp_request=0,isp_apply=0;
  wire cam_pwdn,cam_reset_n,cam_scl,config_done,config_error;
  tri1 cam_sda;
  wire isp_active,isp_busy,isp_done,isp_error,isp_fatal,capture_freeze,af_error;
  wire[15:0] af_status;
  wire wb_pause_request,wb_grant,wb_start,wb_read,wb_busy,wb_done,wb_ack_error;
  wire[23:0] wb_payload;wire[7:0] wb_read_data;
  reg[127:0] request=0;reg request_valid=0,result_received=0;
  wire request_ack,result_send,wb_freeze;wire[127:0]result;
  reg slave_low=0;assign cam_sda=slave_low?1'b0:1'bz;
  reg[7:0] mem[0:65535];reg[7:0]dev,hi,lo,value;
  reg is_read;reg[15:0]addr;
  integer frame_div=0,fw_count=0,af_halts=0,run_reads=0,wb_transactions=0;
  integer n=0,prior_reads=0,legacy_dones=0,wb_done_count=0;
  camera_config_v02 #(.POWER_DOWN_CYCLES(2),.RESET_ASSERT_END_CYCLES(3),
    .RESET_RELEASE_END_CYCLES(5),.POST_RESET_DELAY_CYCLES(2),
    .ROM_ENTRY_COUNT(2),.INIT_MEM_FILE("wb_test_init.mem"),
    .AF_POLL_CYCLES(20),.SCCB_TIMEOUT_CYCLES(100000),
    .FRAME_TIMEOUT_CYCLES(100000),.AF_WAIT_TIMEOUT_CYCLES(2000000)) cam(
    .clk50(clk50),.rst(rst),.frame_boundary(frame_boundary),
    .boot_isp(boot_isp),.isp_request(isp_request),.isp_apply(isp_apply),
    .cam_pwdn(cam_pwdn),.cam_reset_n(cam_reset_n),.cam_scl(cam_scl),.cam_sda(cam_sda),
    .config_done(config_done),.config_error(config_error),.isp_active(isp_active),
    .isp_busy(isp_busy),.isp_done(isp_done),.isp_error(isp_error),.isp_fatal(isp_fatal),
    .capture_freeze(capture_freeze),.af_status(af_status),.af_error(af_error)
`ifndef WB_ARBITRATION_RED
    ,.wb_pause_request(wb_pause_request),.wb_grant(wb_grant),.wb_start(wb_start),
    .wb_read(wb_read),.wb_payload(wb_payload),.wb_busy(wb_busy),.wb_done(wb_done),
    .wb_ack_error(wb_ack_error),.wb_read_data(wb_read_data)
`endif
  );
`ifdef WB_ARBITRATION_RED
  assign wb_grant=0;assign wb_busy=0;assign wb_done=0;assign wb_ack_error=0;assign wb_read_data=0;
`endif
  defparam cam.gen_af.af_i.FW_BYTES=2;
  defparam cam.gen_af.af_i.FW_MEM_FILE="wb_test_af.mem";
  sensor_wb_control #(.AF_WAIT_TIMEOUT_CYCLES(2000000),.FRAME_TIMEOUT_CYCLES(100000)) wb(
    .clk50(clk50),.rst(rst),.abort(1'b0),.request(request),.request_valid(request_valid),
    .request_ack(request_ack),.result(result),.result_send(result_send),.result_received(result_received),
    .pause_request(wb_pause_request),.grant(wb_grant),.config_done(config_done),.isp_busy(isp_busy),
    .frame_boundary(frame_boundary),.capture_freeze(wb_freeze),.sccb_start(wb_start),
    .sccb_read(wb_read),.sccb_payload(wb_payload),.sccb_busy(wb_busy),.sccb_done(wb_done),
    .sccb_ack_error(wb_ack_error),.sccb_read_data(wb_read_data));

  always @(negedge clk50) begin
    frame_boundary=0;
    if(frame_div==999) begin frame_div=0;frame_boundary=1;end else frame_div=frame_div+1;
    if(isp_done) legacy_dones=legacy_dones+1;
    if(wb_done) wb_done_count=wb_done_count+1;
    if(wb_grant && (af_status[15:12]!=12 || cam.controller_owner)) $fatal(1,"grant before AF safe pause");
    if(wb_grant && cam.gen_af.af_i.done) $fatal(1,"WB completion leaked to AF");
    if(wb_grant && isp_busy) $fatal(1,"legacy and WB own engine together");
    if(config_done && (cam_pwdn || !cam_reset_n)) $fatal(1,"runtime reset sensor");
  end
  task get_start;
    begin @(negedge cam_sda); while(cam_scl!==1) @(negedge cam_sda); end
  endtask
  task receive_byte(output[7:0] received);
    integer i;
    begin
      for(i=7;i>=0;i=i-1) begin @(posedge cam_scl);received[i]=cam_sda;end
      @(negedge cam_scl);#1;slave_low=1;
      @(posedge cam_scl);@(negedge cam_scl);#1;slave_low=0;
    end
  endtask
  task get_stop;
    begin @(posedge cam_sda);while(cam_scl!==1) @(posedge cam_sda);end
  endtask
  task send_byte(input[7:0] sent);
    integer i;
    begin
      for(i=7;i>=0;i=i-1) begin
        slave_low=!sent[i];@(posedge cam_scl);@(negedge cam_scl);#1;
      end
      slave_low=0;@(posedge cam_scl);
      if(cam_sda!==1) $fatal(1,"SCCB master missing final NACK");
      @(negedge cam_scl);
    end
  endtask
  initial forever begin
    get_start;
    // Transfer intent is sampled only to distinguish the address-only phase;
    // all address/data bytes and STOP/START/read response traverse real wires.
    is_read=cam.writer_i.read_enable;
    receive_byte(dev);if(dev!==8'h78) $fatal(1,"wrong SCCB write device");
    receive_byte(hi);receive_byte(lo);addr={hi,lo};
    if(is_read) begin
      get_stop;get_start;receive_byte(dev);if(dev!==8'h79) $fatal(1,"wrong SCCB read device");
      if(addr==16'h3029 && af_status[15:12]==12) run_reads=run_reads+1;
      send_byte(mem[addr]);get_stop;
    end else begin
      receive_byte(value);mem[addr]=value;
      if(addr==16'h3000 && value==8'h20) af_halts=af_halts+1;
      if(addr==16'h8000 || addr==16'h8001) fw_count=fw_count+1;
      if(addr==16'h3000 && value==0) mem[16'h3029]=8'h70;
      if(addr==16'h3022 && (value==8'h08 || value==8'h04)) mem[16'h3023]=0;
      get_stop;
    end
    if(wb_grant) wb_transactions=wb_transactions+1;
  end
  task wait_result;
    integer c;
    begin
      c=0;while(!result_send && c<2100000) begin @(negedge clk50);c=c+1;end
      if(!result_send || result[95:64]!==32'h403 || result[63:0]!==64'ha3)
        $fatal(1,"WB must read actual sensor after AF initialization; flags=%h",result[95:64]);
      if(wb_pause_request || wb_freeze || wb_grant) $fatal(1,"ownership not released after drained done");
      result_received=1;repeat(3) @(negedge clk50);result_received=0;
    end
  endtask
  task apply_legacy(input wanted);
    integer c;
    begin
      @(negedge clk50);isp_request=wanted;isp_apply=1;
      @(negedge clk50);isp_apply=0;
      c=0;while(!isp_done && c<500000) begin @(negedge clk50);c=c+1;end
      if(!isp_done || isp_error || isp_fatal || isp_active!==wanted || capture_freeze)
        $fatal(1,"legacy profile failed after WB");
      repeat(3) @(negedge clk50);
    end
  endtask
  initial begin
    mem[16'h5001]=8'ha3;mem[16'h5580]=8'he3;mem[16'h501d]=8'ha5;mem[16'h3406]=8'h5a;
    mem[16'h3029]=8'h00;mem[16'h3023]=8'h00;
    repeat(4) @(negedge clk50);rst=0;
    wait(config_done);@(negedge clk50);
    if({mem[16'h5580],mem[16'h501d],mem[16'h3406]}!==24'h02a55a) $fatal(1,"boot init changed");
    // Request during the initial firmware load: it must finish, not be stolen.
    request={32'd1,32'd0,32'd0,32'h00500101};request_valid=1;
    wait(request_ack);@(negedge clk50);request_valid=0;
    wait_result;
    if(fw_count!=2 || af_halts!=1 || af_status[15:12]!=12 || af_error || wb_done_count!=1 || wb_transactions!=1)
      $fatal(1,"AF startup or WB transaction ownership disturbed");
    if(af_status[7:0]!==8'h00 && af_status[7:0]!==8'h70) $fatal(1,"WB data contaminated AF last_read");
    prior_reads=run_reads;wait(run_reads>prior_reads);wait(!cam.busy);
    // Request during an in-flight AF poll. The poll must drain before grant.
    wait(cam.busy);@(negedge clk50);
    request={32'd2,32'd0,32'd0,32'h00500101};request_valid=1;
    wait(request_ack);@(negedge clk50);request_valid=0;
`ifndef WB_ARBITRATION_RED
    wait(wb_grant);@(negedge clk50);isp_request=1;isp_apply=1;
    @(negedge clk50);isp_apply=0;
`endif
    wait_result;
    if(legacy_dones!=0 || isp_active || isp_busy || capture_freeze) $fatal(1,"legacy APPLY stole WB ownership");
    if(af_status[7:0]!==8'h70) $fatal(1,"WB completion updated AF");
    prior_reads=run_reads;wait(run_reads>prior_reads);
    apply_legacy(1);
    if({mem[16'h5580],mem[16'h501d],mem[16'h3406]}!==24'h064000) $fatal(1,"legacy profile values changed");
    apply_legacy(0);
    if({mem[16'h5580],mem[16'h501d],mem[16'h3406]}!==24'h02a55a || af_halts!=1 || fw_count!=2)
      $fatal(1,"legacy exact baseline restore/AF continuity");
    $display("PASS tb_camera_wb_integration: real SCCB, AF init/poll drain, WB owner, no AF done leak, legacy apply/restore");
    $finish;
  end
  initial begin #100000000; $fatal(1,"integration watchdog");end
endmodule
