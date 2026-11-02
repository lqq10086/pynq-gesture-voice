`timescale 1ns/1ps
// Real init, ISP state machine and AF arbitration; only SCCB wire transport is mocked.
module tb_camera_config_legacy_regression;
    reg clk=0, rst=1, frame_boundary=0, boot_isp=0, request=0, apply=0;
    reg frames_enabled=1;
    wire pwdn, reset_n, scl, config_done, config_error, active, busy, completed;
    wire error, fatal, freeze, af_error;
    wire [15:0] af_status;
    tri1 sda;
    reg mocked_busy=0, mocked_done=0, mocked_ack=0;
    reg [7:0] mocked_data=0;
    reg [7:0] sensor_5580, sensor_501d, sensor_3406;
    reg [23:0] base_rom[0:264];
    reg [7:0] firmware[0:4076];
    integer base_count=0, snapshot_count=0, fw_count=0, halt_count=0, run_reads=0;
    integer failure_mode=0, failed_attempts=0, transactions=0;
    integer frame_div=0, freeze_frames=0, settle_frames=0, previous_run_reads;
    reg freeze_previous=0, boot_done_seen=0;
    reg [2:0] restored_mask=0;
    reg [15:0] address;
    reg [7:0] value;
    reg is_read, is_isp, transaction_fail;
    reg test_pass=0;
    always #10 clk=~clk;
`ifdef SENSOR_LEGACY
    camera_config #(.POWER_DOWN_CYCLES(2),.RESET_ASSERT_END_CYCLES(3),
        .RESET_RELEASE_END_CYCLES(5),.POST_RESET_DELAY_CYCLES(2),
        .RETRY_DELAY_CYCLES(2),.AF_ENABLE(1),.ISP_PROFILE_ENABLE(1)) dut(
        .clk50(clk),.rst(rst),.frame_boundary(frame_boundary),
        .isp_profile_select(boot_isp),.color_profile_request(request),
        .cam_pwdn(pwdn),.cam_reset_n(reset_n),.cam_scl(scl),.cam_sda(sda),
        .config_done(config_done),.config_error(config_error),
        .color_profile_active(active),.color_profile_busy(busy),
        .color_profile_error(error),.capture_freeze(freeze),
        .af_status(af_status),.af_error(af_error));
    assign completed=0;
    assign fatal=0;
`else
    camera_config_v02 #(.POWER_DOWN_CYCLES(2),.RESET_ASSERT_END_CYCLES(3),
        .RESET_RELEASE_END_CYCLES(5),.POST_RESET_DELAY_CYCLES(2),
        .RETRY_DELAY_CYCLES(2),.RUNTIME_MAX_RETRIES(2),
        .SCCB_TIMEOUT_CYCLES(80),.FRAME_TIMEOUT_CYCLES(200),
        .AF_WAIT_TIMEOUT_CYCLES(100000),.AF_POLL_CYCLES(12)) dut(
        .clk50(clk),.rst(rst),.frame_boundary(frame_boundary),
        .boot_isp(boot_isp),.isp_request(request),.isp_apply(apply),
        .cam_pwdn(pwdn),.cam_reset_n(reset_n),.cam_scl(scl),.cam_sda(sda),
        .config_done(config_done),.config_error(config_error),
        .isp_active(active),.isp_busy(busy),.isp_done(completed),
        .isp_error(error),.isp_fatal(fatal),.capture_freeze(freeze),
        .af_status(af_status),.af_error(af_error));
`endif
    initial begin
        $readmemh("ov5640_720p_rgb565.mem",base_rom);
        $readmemh("af_firmware.mem",firmware);
        force dut.busy=mocked_busy;
        force dut.done=mocked_done;
        force dut.ack_error=mocked_ack;
        force dut.read_data=mocked_data;
    end
    always @(negedge clk) begin
        frame_boundary=0;
        if(rst) begin frame_div=0; freeze_frames=0; settle_frames=0; freeze_previous=0; boot_done_seen=0; end
        else begin
            if(config_done) boot_done_seen=1;
            if(boot_done_seen && (!config_done || pwdn || !reset_n))
                $fatal(1,"runtime disturbed config_done or sensor power/reset");
            if(freeze && !freeze_previous) begin freeze_frames=0; settle_frames=0; end
            freeze_previous=freeze;
            if(frames_enabled) begin
                if(frame_div==29) begin
                    frame_div=0; frame_boundary=1;
                    if(freeze) begin freeze_frames=freeze_frames+1; settle_frames=settle_frames+1; end
                end else frame_div=frame_div+1;
            end
        end
    end
    // Capture the actual selected wire-engine request, preserving completion latency.
    always @(negedge clk) begin
        mocked_done=0;
        if(!rst && dut.writer_i.start) begin
            if(mocked_busy) $fatal(1,"overlapping SCCB requests");
            mocked_busy=1; mocked_ack=0; mocked_data=0;
            address=dut.writer_i.payload[23:8]; value=dut.writer_i.payload[7:0];
            is_read=dut.writer_i.read_enable;
            is_isp=(address==16'h5580 || address==16'h501d || address==16'h3406);
            transaction_fail=0; transactions=transactions+1;
            if(base_count<265) begin
                if(is_read || dut.writer_i.payload!=={8'h78,base_rom[base_count]})
                    $fatal(1,"base table order/data changed at %0d",base_count);
                if(address==16'h5580) sensor_5580=value;
                if(address==16'h501d) sensor_501d=value;
                if(address==16'h3406) sensor_3406=value;
                base_count=base_count+1;
            end else if(is_isp) begin
                if(!config_done && snapshot_count<3) begin
                    if(!is_read || (snapshot_count==0 && address!=16'h5580) ||
                        (snapshot_count==1 && address!=16'h501d) ||
                        (snapshot_count==2 && address!=16'h3406))
                        $fatal(1,"missing ordered sensor baseline snapshot before ISP writes");
                    snapshot_count=snapshot_count+1;
                end
                if(config_done && (!freeze || freeze_frames<2))
                    $fatal(1,"runtime SCCB before capture freeze and two raw frame boundaries");
                if(config_done) settle_frames=0;
                if(config_done && !is_read) begin
                    if(address==16'h5580 && value==8'h02) restored_mask[0]=1;
                    if(address==16'h501d && value==8'ha5) restored_mask[1]=1;
                    if(address==16'h3406 && value==8'h5a) restored_mask[2]=1;
                end
                if(config_done && failure_mode==6 && is_read && address==16'h3406)
                    frames_enabled=0;
                if(config_done && failure_mode==4) begin
                    wait(rst); mocked_busy=0;
                end else begin
                    if(config_done && address==16'h501d && !is_read &&
                        ((failure_mode==1 && value==8'h40) || failure_mode==3)) begin
                        mocked_ack=1; transaction_fail=1; failed_attempts=failed_attempts+1;
                    end
                    if(is_read) begin
                        case(address)
                            16'h5580: mocked_data=sensor_5580;
                            16'h501d: mocked_data=sensor_501d;
                            16'h3406: mocked_data=sensor_3406;
                        endcase
                        if(config_done && failure_mode==2 && address==16'h501d && failed_attempts<3) begin
                            mocked_data=8'h99; failed_attempts=failed_attempts+1;
                        end
                    end else if(!transaction_fail) begin
                        case(address)
                            16'h5580: sensor_5580=value;
                            16'h501d: sensor_501d=value;
                            16'h3406: sensor_3406=value;
                        endcase
                    end
                end
            end else begin
                if(!config_done || snapshot_count!=3) $fatal(1,"AF enabled before boot snapshot/profile completion");
                if(address==16'h3000 && value==8'h20 && !is_read) halt_count=halt_count+1;
                if(address>=16'h8000 && address<16'h9000) begin
                    if(address!==(16'h8000+fw_count) || value!==firmware[fw_count])
                        $fatal(1,"AF firmware restarted or changed at %0d",fw_count);
                    fw_count=fw_count+1;
                end
                if(is_read && address==16'h3029) begin
                    mocked_data=(af_status[15:12]==12)?8'h10:8'h70;
                    if(af_status[15:12]==12) run_reads=run_reads+1;
                end
                if(is_read && address==16'h3023) mocked_data=0;
                if(failure_mode==7 && address==16'h3000) mocked_ack=1;
                if(failure_mode==5) begin wait(rst); mocked_busy=0; end
            end
            repeat(3) @(negedge clk);
            mocked_busy=0; mocked_done=1;
        end
    end
    task reset_case;
        input profile;
        begin
            @(negedge clk); rst=1; boot_isp=!profile; apply=0; request=0;
            repeat(8) @(negedge clk);
            sensor_5580=8'he3; sensor_501d=8'ha5; sensor_3406=8'h5a;
            base_count=0; snapshot_count=0; fw_count=0; halt_count=0; run_reads=0;
            failed_attempts=0; failure_mode=0; transactions=0; frames_enabled=1;
            mocked_busy=0; mocked_done=0; mocked_ack=0;
            rst=0;
            // The top's switch synchronizer settles after reset, before power-up ends.
            repeat(2) @(negedge clk); boot_isp=profile;
            wait(config_done); #1;
            if(snapshot_count!=3) $fatal(1,"missing baseline snapshot: got %0d sensor reads",snapshot_count);
            if(active!==profile || config_error || error || fatal) $fatal(1,"boot profile failed");
            if(profile) begin
                if({sensor_5580,sensor_501d,sensor_3406}!==24'h064000) $fatal(1,"wrong boot1 values");
            end else if({sensor_5580,sensor_501d,sensor_3406}!==24'h02a55a)
                $fatal(1,"boot0 changed sensor baseline");
        end
    endtask
    task submit;
        input profile;
        begin
            @(negedge clk); request=profile; apply=1; restored_mask=0;
            @(negedge clk); apply=0;
            if(!busy || !freeze) $fatal(1,"request did not freeze capture immediately");
        end
    endtask
    task completion;
        input wanted_active, wanted_error, wanted_fatal;
        begin
            wait(completed); #1;
            if(busy || active!==wanted_active || error!==wanted_error || fatal!==wanted_fatal)
                $fatal(1,"completion status mismatch active=%b error=%b fatal=%b",active,error,fatal);
            if(freeze!==wanted_fatal) $fatal(1,"unsafe freeze release or missing release");
            if(!wanted_fatal && settle_frames<3) $fatal(1,"released before three raw settling frames");
            if(!wanted_fatal && halt_count!=1) $fatal(1,"AF restarted");
            @(negedge clk); @(negedge clk);
            if(completed) $fatal(1,"done is not a single cycle pulse");
        end
    endtask
    initial begin
        reset_case(0);
        // Apply during firmware load: AF must finish the original command sequence first.
        submit(1);
        // A second apply while busy must not replace the accepted request.
        @(negedge clk); request=0; apply=1;
        @(negedge clk); apply=0;
        completion(1,0,0);
        if(fw_count!=4077 || af_status[15:12]!=12) $fatal(1,"runtime stole AF initialization");
        previous_run_reads=run_reads; wait(run_reads>previous_run_reads);
        submit(0); completion(0,0,0);
        if({sensor_5580,sensor_501d,sensor_3406}!==24'h02a55a) $fatal(1,"profile0 guessed defaults");
        previous_run_reads=run_reads; wait(run_reads>previous_run_reads);
        failure_mode=1; failed_attempts=0; submit(1); completion(0,1,0);
        if(failed_attempts!=3 || restored_mask!=7 || {sensor_5580,sensor_501d,sensor_3406}!==24'h02a55a)
            $fatal(1,"NACK retries were unbounded or rollback incomplete");
        failure_mode=2; failed_attempts=0; submit(1); completion(0,1,0);
        if(failed_attempts!=3 || restored_mask!=7 || {sensor_5580,sensor_501d,sensor_3406}!==24'h02a55a)
            $fatal(1,"readback mismatch ignored or rollback incomplete");
        failure_mode=0; submit(1); completion(1,0,0);
        $display("PASS boot0 snapshot; runtime 0->1->0; AF initialize/pause/resume; NACK and readback rollback");
        reset_case(1); wait(af_status[15:12]==12);
        submit(0); completion(0,0,0);
        if({sensor_5580,sensor_501d,sensor_3406}!==24'h02a55a) $fatal(1,"boot1 lost original snapshot");
        failure_mode=3; failed_attempts=0; submit(1); completion(0,1,1);
        if(failed_attempts!=6 || restored_mask!=7) $fatal(1,"failed rollback not bounded/complete");
        repeat(300) @(negedge clk);
        if(!fatal || !freeze || busy) $fatal(1,"fatal state not stable");
        $display("PASS boot1 snapshot restoration and failed rollback frozen fatal");
        reset_case(0); wait(af_status[15:12]==12);
        frames_enabled=0; submit(1); completion(0,1,1);
        $display("PASS missing raw frame bounded timeout");
        reset_case(0); wait(af_status[15:12]==12);
        failure_mode=4; submit(1); completion(0,1,1);
        $display("PASS stuck SCCB bounded timeout with frozen capture");
        reset_case(0); failure_mode=7; wait(af_error);
        submit(1); completion(1,0,0);
        if(!af_error || fw_count!=0) $fatal(1,"ISP error-state handoff restarted AF");
        $display("PASS AF ERROR transaction boundary handoff without restart");
        reset_case(0); failure_mode=5; submit(1); completion(0,1,1);
        $display("PASS stuck AF transaction bounded wait timeout");
        reset_case(0); wait(af_status[15:12]==12);
        failure_mode=6; submit(1); completion(0,1,1);
        if({sensor_5580,sensor_501d,sensor_3406}!==24'h064000)
            $fatal(1,"settling timeout test failed to finish target writes");
        $display("PASS missing settling frames remain frozen with terminal status");
        test_pass=1; $display("PASS camera_config_v02 all sensor-control cases"); $finish;
    end
    initial begin #30000000; $fatal(1,"camera_config_v02 test timeout"); end
endmodule

