`timescale 1ns/1ps

module system_top (
    inout  wire [14:0] DDR_addr,
    inout  wire [2:0]  DDR_ba,
    inout  wire        DDR_cas_n,
    inout  wire        DDR_ck_n,
    inout  wire        DDR_ck_p,
    inout  wire        DDR_cke,
    inout  wire        DDR_cs_n,
    inout  wire [3:0]  DDR_dm,
    inout  wire [31:0] DDR_dq,
    inout  wire [3:0]  DDR_dqs_n,
    inout  wire [3:0]  DDR_dqs_p,
    inout  wire        DDR_odt,
    inout  wire        DDR_ras_n,
    inout  wire        DDR_reset_n,
    inout  wire        DDR_we_n,
    inout  wire        FIXED_IO_ddr_vrn,
    inout  wire        FIXED_IO_ddr_vrp,
    inout  wire [53:0] FIXED_IO_mio,
    inout  wire        FIXED_IO_ps_clk,
    inout  wire        FIXED_IO_ps_porb,
    inout  wire        FIXED_IO_ps_srstb,

    input  wire        clk50,
    input  wire        color_sw0,
    input  wire        color_sw1,
    input  wire        cam_pclk,
    input  wire        cam_vsync,
    input  wire        cam_href,
    input  wire [7:0]  cam_data,
    output wire        cam_pwdn,
    output wire        cam_reset_n,
    output wire        cam_scl,
    inout  wire        cam_sda,
    input  wire        cam_flash,

    output wire        hdmi_clk_p,
    output wire        hdmi_clk_n,
    output wire [2:0]  hdmi_data_p,
    output wire [2:0]  hdmi_data_n,
    output wire        hdmi_out_en,
    output wire [3:0]  led
);
    reg [20:0] poweron_count = 21'd0;
    wire reset50 = !(&poweron_count);
    always @(posedge clk50) begin
        if (!(&poweron_count))
            poweron_count <= poweron_count + 1'b1;
    end

    wire pix_clk;
    wire pix_clk_5x;
    wire clk_locked;
    (* ASYNC_REG = "TRUE" *) reg [3:0] pix_reset_pipe = 4'b1111;
    wire pix_reset = pix_reset_pipe[3];

    clk_gen clocks_i (
        .clk50(clk50),
        .pix_clk(pix_clk),
        .pix_clk_5x(pix_clk_5x),
        .locked(clk_locked)
    );

    always @(posedge pix_clk or negedge clk_locked) begin
        if (!clk_locked)
            pix_reset_pipe <= 4'b1111;
        else
            pix_reset_pipe <= {pix_reset_pipe[2:0], 1'b0};
    end

    wire config_done;
    wire config_error;
    wire sensor_color_profile_active;
    wire sensor_color_profile_busy;
    wire sensor_color_profile_error;
    wire color_capture_freeze;
    wire config_capture_freeze, wb_capture_freeze;
    assign color_capture_freeze = config_capture_freeze || wb_capture_freeze;
    wire wb_pause_request, wb_grant, wb_start, wb_read, wb_busy, wb_done, wb_ack_error;
    wire [23:0] wb_payload;
    wire [7:0] wb_read_data;
    wire [127:0] wb_command, wb_result, wb_request50, wb_response50;
    wire wb_send, wb_received, wb_result_valid;
    wire wb_abort, wb_abort50;
    wire wb_request_valid50, wb_request_ack50, wb_response_send50, wb_response_received50;
    wire [15:0] af_status;
    wire af_error;
    wire [1:0] color_switches;
    wire boot_isp, boot_profile_valid;
    wire isp_request,isp_apply,isp_done,isp_fatal;
    wire pl_request50;
    wire pl_commit_send,pl_commit_received;
    color_mode_control mode_i(.clk(clk50),.rst(reset50),
        .sw0_async(color_sw0),.sw1_async(color_sw1),.switches(color_switches),
        .boot_isp(boot_isp),.boot_valid(boot_profile_valid));

    (* ASYNC_REG = "TRUE" *) reg [2:0] camera_vsync_sync = 3'b000;
    always @(posedge clk50) begin
        if (reset50)
            camera_vsync_sync <= 3'b000;
        else
            camera_vsync_sync <= {camera_vsync_sync[1:0], cam_vsync};
    end
    wire camera_frame_boundary50 =
        camera_vsync_sync[1] && !camera_vsync_sync[2];

    camera_config_v02 camera_config_i (
        .clk50(clk50),
        .rst(reset50 || !boot_profile_valid),
        .frame_boundary(camera_frame_boundary50),
        .boot_isp(boot_isp),.isp_request(isp_request),.isp_apply(isp_apply),
        .cam_pwdn(cam_pwdn),
        .cam_reset_n(cam_reset_n),
        .cam_scl(cam_scl),
        .cam_sda(cam_sda),
        .config_done(config_done),
        .config_error(config_error),
        .isp_active(sensor_color_profile_active),
        .isp_busy(sensor_color_profile_busy),
        .isp_error(sensor_color_profile_error),.isp_done(isp_done),.isp_fatal(isp_fatal),
        .capture_freeze(config_capture_freeze),
        .wb_pause_request(wb_pause_request), .wb_grant(wb_grant),
        .wb_start(wb_start), .wb_read(wb_read), .wb_payload(wb_payload),
        .wb_busy(wb_busy), .wb_done(wb_done), .wb_ack_error(wb_ack_error),
        .wb_read_data(wb_read_data),
        .af_status(af_status),
        .af_error(af_error)
    );
    wire cam_pclk_raw,cam_pclk_i,cam_pclk_shifted,cam_clk_locked,cam_clk_feedback,cam_clk_feedback_buf;
    BUFG cam_pclk_buf_i (.I(cam_pclk), .O(cam_pclk_raw));
    // A delayed rising sample gives 3/4 period setup and 1/4 period hold.
    // Keep the external falling-edge 8 ns max / 0 ns min I/O budget in XDC.
    MMCME2_BASE #(.BANDWIDTH("OPTIMIZED"),.CLKIN1_PERIOD(12.048192771),
        .DIVCLK_DIVIDE(1),.CLKFBOUT_MULT_F(8.0),.CLKOUT0_DIVIDE_F(8.0),
        .CLKOUT0_PHASE(90.0),.STARTUP_WAIT("FALSE")) camera_sample_mmcm_i(
        .CLKIN1(cam_pclk_raw),.CLKFBIN(cam_clk_feedback_buf),.CLKFBOUT(cam_clk_feedback),
        .CLKOUT0(cam_pclk_shifted),.LOCKED(cam_clk_locked),.PWRDWN(1'b0),.RST(!config_done));
    BUFG camera_feedback_buf_i(.I(cam_clk_feedback),.O(cam_clk_feedback_buf));
    BUFG camera_sample_buf_i(.I(cam_pclk_shifted),.O(cam_pclk_i));

    wire capture_enable_control;
    (* ASYNC_REG = "TRUE" *) reg [1:0] capture_enable_sync = 2'b00;
    (* ASYNC_REG = "TRUE" *) reg [1:0] color_capture_freeze_sync = 2'b00;

    reg [31:0] cam_pclk_counter = 32'd0;
    always @(posedge cam_pclk_i)
        cam_pclk_counter <= cam_pclk_counter + 1'b1;

    (* ASYNC_REG = "TRUE" *) reg [2:0] cam_reset_pipe = 3'b111;
    wire camera_clock_ready = config_done && cam_clk_locked;
    always @(posedge cam_pclk_i or negedge camera_clock_ready) begin
        if (!camera_clock_ready)
            cam_reset_pipe <= 3'b111;
        else
            cam_reset_pipe <= {cam_reset_pipe[1:0], 1'b0};
    end
    wire cam_capture_reset = cam_reset_pipe[2];

    always @(posedge cam_pclk_i or posedge cam_capture_reset) begin
        if (cam_capture_reset)
            capture_enable_sync <= 2'b00;
        else
            capture_enable_sync <= {
                capture_enable_sync[0], capture_enable_control
            };
    end

    always @(posedge cam_pclk_i or posedge cam_capture_reset) begin
        if (cam_capture_reset)
            color_capture_freeze_sync <= 2'b00;
        else
            color_capture_freeze_sync <= {
                color_capture_freeze_sync[0], color_capture_freeze
            };
    end
    // Register the combined level in the camera clock domain. Besides giving
    // the DVP capture path a glitch-free control, this avoids presenting
    // combinational logic to the downstream AXI GPIO CDC synchronizer.
    reg capture_enable_cam = 1'b0;
    always @(posedge cam_pclk_i or posedge cam_capture_reset) begin
        if (cam_capture_reset)
            capture_enable_cam <= 1'b0;
        else
            capture_enable_cam <=
                capture_enable_sync[1] && !color_capture_freeze_sync[1];
    end

    wire [15:0] cam_axis_tdata;
    wire [1:0]  cam_axis_tkeep;
    wire        cam_axis_tvalid;
    wire        cam_axis_tready;
    wire        cam_axis_tuser;
    wire        cam_axis_tlast;
    wire        camera_frame_start;
    wire        camera_frame_done;
    wire        camera_frame_seen;
    wire        camera_fifo_overflow;
    wire        camera_addr_overflow;
    wire [15:0] camera_complete_frame_count;
    wire [15:0] camera_dropped_frame_count;
    wire [1:0] camera_drop_events;
    wire camera_overflow_event;

    camera_dvp_to_axis_rgb565 camera_axis_i (
        .pclk(cam_pclk_i),
        .rst(cam_capture_reset),
        .capture_enable(capture_enable_cam),
        .vsync(cam_vsync),
        .href(cam_href),
        .data(cam_data),
        .m_axis_tdata(cam_axis_tdata),
        .m_axis_tkeep(cam_axis_tkeep),
        .m_axis_tvalid(cam_axis_tvalid),
        .m_axis_tready(cam_axis_tready),
        .m_axis_tuser(cam_axis_tuser),
        .m_axis_tlast(cam_axis_tlast),
        .frame_start(camera_frame_start),
        .frame_done(camera_frame_done),
        .frame_seen(camera_frame_seen),
        .fifo_overflow(camera_fifo_overflow),
        .addr_overflow(camera_addr_overflow),
        .accepted_pixel_count(),
        .complete_frame_count(camera_complete_frame_count),
        .dropped_frame_count(camera_dropped_frame_count),
        .drop_events(camera_drop_events),.overflow_event(camera_overflow_event)
    );

    // Count every sensor VSYNC independently of software capture ownership.
    reg [13:0] sensor_frame_count = 14'd0;
    always @(posedge cam_pclk_i) begin
        if (cam_capture_reset)
            sensor_frame_count <= 14'd0;
        else if (camera_frame_start)
            sensor_frame_count <= sensor_frame_count + 1'b1;
    end

    wire [15:0] video_axis_tdata;
    wire [1:0]  video_axis_tkeep;
    wire        video_axis_tvalid;
    wire        video_axis_tready;
    wire        video_axis_tuser;
    wire        video_axis_tlast;
    wire        video_running;
    wire        video_underflow;
    wire        video_sync_error;
    // Bit 31 is a layout marker; bits 30:17 expose the raw sensor frame count.
    wire [31:0] pl_status = {
        1'b1,
        sensor_frame_count,
        capture_enable_cam,
        cam_scl,
        cam_sda,
        cam_flash,
        cam_href,
        cam_vsync,
        camera_frame_done,
        camera_frame_start,
        video_sync_error,
        video_underflow,
        video_running,
        camera_addr_overflow,
        camera_fifo_overflow,
        camera_frame_seen,
        config_error,
        config_done,
        clk_locked
    };
    wire fpga_color_request_pix;
    wire fpga_color_active_pix;
    wire fpga_color_pending_pix;
    wire spatial_frame_request, global_frame_request, pl_switch_request;
    pl_color_frame_control pl_mode_i(.clk(pix_clk),.rst(pix_reset),
        .request_async(pl_request50),.valid(video_axis_tvalid),
        .ready(video_axis_tready),.sof(video_axis_tuser),
        .spatial_request(spatial_frame_request),.global_request(global_frame_request),
        .requested(pl_switch_request));
    wire PL_COLOR_BYPASS = !fpga_color_active_pix;

    wire control_clk,control_resetn,color_send,color_received,color_result_valid;
    wire switch_changed50, pending_status_pix, fpga_color_active_axi;
    color_status_cdc status_cdc_i (
        .clk50(clk50),.rst50(reset50),.pix_clk(pix_clk),.pix_rst(pix_reset),
        .axi_clk(control_clk),.switch_isp(color_switches[1]),.boot_isp(boot_isp),
        .pending_pix(fpga_color_pending_pix),.requested_pix(pl_switch_request),
        .active_pix(fpga_color_active_pix),.switch_changed50(switch_changed50),
        .pending_status_pix(pending_status_pix),.active_axi(fpga_color_active_axi)
    );
    // Keep the V1.7 diagnostic layout. Duplicate active only after its CDC.
    wire [31:0] pl_counts = {
        sensor_color_profile_error,
        pending_status_pix,
        fpga_color_active_axi,
        pl_switch_request,
        sensor_color_profile_active,fpga_color_active_axi,
        af_status,
        color_switches[1],
        switch_changed50,
        8'h25
    };

    wire [3:0] color_command;
    wire [31:0] color_result;
    sensor_wb_bridge wb_bridge_i(
        .axi_clk(control_clk), .clk50(clk50),
        .abort(wb_abort), .abort50(wb_abort50),
        .command(wb_command), .command_send(wb_send), .command_received(wb_received),
        .result(wb_result), .result_valid(wb_result_valid),
        .request50(wb_request50), .request_valid50(wb_request_valid50), .request_ack50(wb_request_ack50),
        .response50(wb_response50), .response_send50(wb_response_send50), .response_received50(wb_response_received50));
    sensor_wb_control wb_control_i(
        .clk50(clk50), .rst(reset50 || !boot_profile_valid),
        .abort(wb_abort50),
        .request(wb_request50), .request_valid(wb_request_valid50), .request_ack(wb_request_ack50),
        .result(wb_response50), .result_send(wb_response_send50), .result_received(wb_response_received50),
        .pause_request(wb_pause_request), .grant(wb_grant),
        .config_done(config_done), .isp_busy(sensor_color_profile_busy),
        .frame_boundary(camera_frame_boundary50), .capture_freeze(wb_capture_freeze),
        .sccb_start(wb_start), .sccb_read(wb_read), .sccb_payload(wb_payload),
        .sccb_busy(wb_busy), .sccb_done(wb_done), .sccb_ack_error(wb_ack_error), .sccb_read_data(wb_read_data));
    wire [31:0] bbox_word0,bbox_word1,bbox_word2,bbox_word3;
    wire [31:0] bbox2_word0,bbox2_word1,bbox2_word2;
    wire bbox_ack;
    camera_control_bridge control_bridge_i(
        .axi_clk(control_clk),.clk50(clk50),.rst50(reset50),
        .command(color_command),.command_send(color_send),.command_received(color_received),
        .result(color_result),.result_valid(color_result_valid),
        .switches(color_switches),.boot_isp(boot_isp),.config_done(config_done),.config_error(config_error),
        .isp_active(sensor_color_profile_active),.isp_busy(sensor_color_profile_busy),.isp_done(isp_done),
        .isp_error(sensor_color_profile_error),.isp_fatal(isp_fatal),.af_error(af_error),
        .isp_request(isp_request),.isp_apply(isp_apply),.pl_request(pl_request50),
        .pl_active_async(fpga_color_active_pix),.pl_commit_send(pl_commit_send),.pl_commit_received(pl_commit_received));

    wire [63:0] ui_axis_tdata;
    wire [7:0] ui_axis_tkeep;
    wire ui_axis_tvalid,ui_axis_tready,ui_axis_tuser,ui_axis_tlast;
    wire [127:0] ui_status;
    system_wrapper system_i (
        .M_AXIS_UI_tdata(ui_axis_tdata),.M_AXIS_UI_tkeep(ui_axis_tkeep),
        .M_AXIS_UI_tvalid(ui_axis_tvalid),.M_AXIS_UI_tready(ui_axis_tready),
        .M_AXIS_UI_tuser(ui_axis_tuser),.M_AXIS_UI_tlast(ui_axis_tlast),
        .UI_STATUS0(ui_status[31:0]),.UI_STATUS1(ui_status[63:32]),
        .UI_STATUS2(ui_status[95:64]),.UI_STATUS3(ui_status[127:96]),
        .CONTROL_CLK(control_clk),.CONTROL_RESETN(control_resetn),
        .STATS_CAM_RESET(cam_capture_reset),.STATS_CAM_VSYNC(camera_frame_start),.STATS_CAM_DONE(camera_frame_done),
        .STATS_CAM_DROP_EVENTS(camera_drop_events),.STATS_CAM_OVERFLOW_EVENT(camera_overflow_event),
        .COLOR_COMMAND(color_command),.COLOR_SEND(color_send),.COLOR_RECEIVED(color_received),
        .COLOR_RESULT(color_result),.COLOR_RESULT_VALID(color_result_valid),
        .WB_COMMAND(wb_command), .WB_SEND(wb_send), .WB_RECEIVED(wb_received),
        .WB_RESULT(wb_result), .WB_RESULT_VALID(wb_result_valid),
        .WB_ABORT(wb_abort),
        .BBOX_WORD0(bbox_word0),.BBOX_WORD1(bbox_word1),
        .BBOX_WORD2(bbox_word2),.BBOX_WORD3(bbox_word3),.BBOX_ACK(bbox_ack),
        .BBOX2_WORD0(bbox2_word0),.BBOX2_WORD1(bbox2_word1),.BBOX2_WORD2(bbox2_word2),
        .CAPTURE_ARM(capture_enable_control),
        .DDR_addr(DDR_addr),
        .DDR_ba(DDR_ba),
        .DDR_cas_n(DDR_cas_n),
        .DDR_ck_n(DDR_ck_n),
        .DDR_ck_p(DDR_ck_p),
        .DDR_cke(DDR_cke),
        .DDR_cs_n(DDR_cs_n),
        .DDR_dm(DDR_dm),
        .DDR_dq(DDR_dq),
        .DDR_dqs_n(DDR_dqs_n),
        .DDR_dqs_p(DDR_dqs_p),
        .DDR_odt(DDR_odt),
        .DDR_ras_n(DDR_ras_n),
        .DDR_reset_n(DDR_reset_n),
        .DDR_we_n(DDR_we_n),
        .FIXED_IO_ddr_vrn(FIXED_IO_ddr_vrn),
        .FIXED_IO_ddr_vrp(FIXED_IO_ddr_vrp),
        .FIXED_IO_mio(FIXED_IO_mio),
        .FIXED_IO_ps_clk(FIXED_IO_ps_clk),
        .FIXED_IO_ps_porb(FIXED_IO_ps_porb),
        .FIXED_IO_ps_srstb(FIXED_IO_ps_srstb),
        .PL_COUNTS(pl_counts),
        .PL_STATUS(pl_status),
        .S_AXIS_CAM_tdata(cam_axis_tdata),
        .S_AXIS_CAM_tkeep(cam_axis_tkeep),
        .S_AXIS_CAM_tlast(cam_axis_tlast),
        .S_AXIS_CAM_tready(cam_axis_tready),
        .S_AXIS_CAM_tuser(cam_axis_tuser),
        .S_AXIS_CAM_tvalid(cam_axis_tvalid),
        .M_AXIS_VIDEO_tdata(video_axis_tdata),
        .M_AXIS_VIDEO_tkeep(video_axis_tkeep),
        .M_AXIS_VIDEO_tlast(video_axis_tlast),
        .M_AXIS_VIDEO_tready(video_axis_tready),
        .M_AXIS_VIDEO_tuser(video_axis_tuser),
        .M_AXIS_VIDEO_tvalid(video_axis_tvalid),
        .cam_axis_aclk(cam_pclk_i),
        .cam_axis_aresetn(!cam_capture_reset),
        .video_axis_aclk(pix_clk),
        .video_axis_aresetn(!pix_reset)
    );

    wire [7:0] video_red;
    wire [7:0] video_green;
    wire [7:0] video_blue;
    wire video_de;
    wire video_hsync;
    wire video_vsync;
    wire video_frame_start;

    wire [15:0] fpga_color_tag,video_pixel_tag;
    wire [15:0] mirror_data,blend_data,blend_tag;
    wire mirror_valid,mirror_ready,mirror_user,mirror_last,mirror_error;
    wire blend_valid,blend_ready,blend_user,blend_last;
    wire [31:0] ui_rejected,ui_presented_count,ui_video_count;
    wire [15:0] ui_presented_tag;
    axis_mirror_rgb565 mirror_i(
        .clk(pix_clk),.rst(pix_reset),
        .s_axis_tdata(video_axis_tdata),.s_axis_tvalid(video_axis_tvalid),
        .s_axis_tready(video_axis_tready),.s_axis_tuser(video_axis_tuser),.s_axis_tlast(video_axis_tlast),
        .m_axis_tdata(mirror_data),.m_axis_tvalid(mirror_valid),
        .m_axis_tready(mirror_ready),.m_axis_tuser(mirror_user),.m_axis_tlast(mirror_last),
        .sync_error(mirror_error));
    wire [15:0] fpga_color_axis_tdata;
    wire [1:0]  fpga_color_axis_tkeep;
    wire         fpga_color_axis_tvalid;
    wire         fpga_color_axis_tready;
    wire         fpga_color_axis_tuser;
    wire         fpga_color_axis_tlast;

    wire [15:0] fpga_spatial_axis_tdata;
    wire [1:0]  fpga_spatial_axis_tkeep;
    wire         fpga_spatial_axis_tvalid;
    wire         fpga_spatial_axis_tready;
    wire         fpga_spatial_axis_tuser;
    wire         fpga_spatial_axis_tlast;

    fpga_spatial_chroma_correction #(
        .FORCE_CORRECTION(0)
    ) fpga_spatial_i (
        .clk(pix_clk),
        .rst(pix_reset),
        .profile_request(1'b0),
        .s_axis_tdata(mirror_data),
        .s_axis_tkeep(2'b11),
        .s_axis_tvalid(mirror_valid),
        .s_axis_tready(mirror_ready),
        .s_axis_tuser(mirror_user),
        .s_axis_tlast(mirror_last),
        .m_axis_tdata(fpga_spatial_axis_tdata),
        .m_axis_tkeep(fpga_spatial_axis_tkeep),
        .m_axis_tvalid(fpga_spatial_axis_tvalid),
        .m_axis_tready(fpga_spatial_axis_tready),
        .m_axis_tuser(fpga_spatial_axis_tuser),
        .m_axis_tlast(fpga_spatial_axis_tlast)
    );

    axis_ui_blend ui_blend_i(
        .clk(pix_clk),.rst(pix_reset),
        .s_axis_tdata(fpga_spatial_axis_tdata),.s_axis_tvalid(fpga_spatial_axis_tvalid),
        .s_axis_tready(fpga_spatial_axis_tready),.s_axis_tuser(fpga_spatial_axis_tuser),.s_axis_tlast(fpga_spatial_axis_tlast),
        .ui_tdata(ui_axis_tdata),.ui_tvalid(ui_axis_tvalid),.ui_tready(ui_axis_tready),
        .ui_tuser(ui_axis_tuser),.ui_tlast(ui_axis_tlast),
        .m_axis_tdata(blend_data),.m_axis_tvalid(blend_valid),.m_axis_tready(blend_ready),
        .m_axis_tuser(blend_user),.m_axis_tlast(blend_last),.m_axis_tag(blend_tag),
        .rejected_frames(ui_rejected));
    ui_present_monitor ui_present_i(
        .clk(pix_clk),.rst(pix_reset),.de(video_de),.frame_start(video_frame_start),
        .pixel_tag(video_pixel_tag),.presented_tag(ui_presented_tag),
        .presented_count(ui_presented_count),.video_count(ui_video_count));
    ui_status_cdc ui_status_i(.pix_clk(pix_clk),.ctrl_clk(control_clk),
        .live_data({ui_rejected,ui_video_count,ui_presented_count,8'h45,7'd0,mirror_error,ui_presented_tag}),
        .status(ui_status));

    fpga_color_pipeline #(
        .FORCE_CORRECTION(0)
    ) fpga_color_i (
        .clk(pix_clk),
        .rst(pix_reset),
        .profile_request_async(global_frame_request),
        .profile_request(fpga_color_request_pix),
        .profile_active(fpga_color_active_pix),
        .profile_pending(fpga_color_pending_pix),
        .s_axis_tdata(blend_data),.s_axis_tag(blend_tag),.m_axis_tag(fpga_color_tag),
        .s_axis_tkeep(2'b11),
        .s_axis_tvalid(blend_valid),
        .s_axis_tready(blend_ready),
        .s_axis_tuser(blend_user),
        .s_axis_tlast(blend_last),
        .m_axis_tdata(fpga_color_axis_tdata),
        .m_axis_tkeep(fpga_color_axis_tkeep),
        .m_axis_tvalid(fpga_color_axis_tvalid),
        .m_axis_tready(fpga_color_axis_tready),
        .m_axis_tuser(fpga_color_axis_tuser),
        .m_axis_tlast(fpga_color_axis_tlast)
    );

    pl_color_commit color_commit_i(
        .clk50(clk50),.pix_clk(pix_clk),.pix_reset(pix_reset),.target(pl_request50),
        .send(pl_commit_send),.received(pl_commit_received),
        .upstream_sof(video_axis_tvalid && video_axis_tready && video_axis_tuser),
        .upstream_mode(spatial_frame_request),
        .downstream_sof(fpga_color_axis_tvalid && fpga_color_axis_tready && fpga_color_axis_tuser));

    axis_rgb565_video_out #(
        // The legacy position-dependent PL gains remain permanently bypassed.
        // Global processing is exclusively in fpga_color_pipeline above.
        .COLOR_CORRECTION_ENABLE(0)
    ) video_out_i (
        .pix_clk(pix_clk),
        .rst(pix_reset),
        .s_axis_tdata(fpga_color_axis_tdata),.s_axis_tag(fpga_color_tag),.pixel_tag(video_pixel_tag),
        .s_axis_tvalid(fpga_color_axis_tvalid),
        .s_axis_tready(fpga_color_axis_tready),
        .s_axis_tuser(fpga_color_axis_tuser),
        .s_axis_tlast(fpga_color_axis_tlast),
        .red(video_red),
        .green(video_green),
        .blue(video_blue),
        .de(video_de),
        .hsync(video_hsync),
        .vsync(video_vsync),
        .frame_start(video_frame_start),
        .video_running(video_running),
        .underflow_error(video_underflow),
        .sync_error(video_sync_error),
        .displayed_frame_count(),
        .underflow_count()
    );

    wire tmds_clk;
    wire [2:0] tmds_data;
    wire [7:0] overlay_red,overlay_green,overlay_blue;
    bbox_overlay bbox_i (
        .ctrl_clk(control_clk),.ctrl_rst_n(control_resetn),
        .pix_clk(pix_clk),.pix_rst(pix_reset),
        .shadow0(bbox_word0),.shadow1(bbox_word1),
        .shadow2(bbox_word2),.shadow3(bbox_word3),.ack_toggle(bbox_ack),
        .shadow4(bbox2_word0),.shadow5(bbox2_word1),.shadow6(bbox2_word2),
        .de(video_de),.frame_start(video_frame_start),
        .red_in(video_red),.green_in(video_green),.blue_in(video_blue),
        .red_out(overlay_red),.green_out(overlay_green),.blue_out(overlay_blue)
    );
    dvi_tx dvi_i (
        .pix_clk(pix_clk),
        .pix_clk_5x(pix_clk_5x),
        .rst(pix_reset),
        .red(overlay_red),
        .green(overlay_green),
        .blue(overlay_blue),
        .de(video_de),
        .hsync(video_hsync),
        .vsync(video_vsync),
        .tmds_clk(tmds_clk),
        .tmds_data(tmds_data)
    );

    OBUFDS hdmi_clk_buf_i (.I(tmds_clk), .O(hdmi_clk_p), .OB(hdmi_clk_n));
    OBUFDS hdmi_d0_buf_i (.I(tmds_data[0]), .O(hdmi_data_p[0]), .OB(hdmi_data_n[0]));
    OBUFDS hdmi_d1_buf_i (.I(tmds_data[1]), .O(hdmi_data_p[1]), .OB(hdmi_data_n[1]));
    OBUFDS hdmi_d2_buf_i (.I(tmds_data[2]), .O(hdmi_data_p[2]), .OB(hdmi_data_n[2]));

    assign hdmi_out_en = 1'b1;
    assign led[0] = !clk_locked;
    assign led[1] = !config_done;
    assign led[2] = !video_running;
    assign led[3] = !(camera_fifo_overflow || camera_addr_overflow ||
                      video_underflow || video_sync_error ||
                      sensor_color_profile_error || af_error ||
                      (config_error && !config_done));
endmodule
