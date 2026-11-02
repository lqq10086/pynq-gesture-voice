`timescale 1ns/1ps

// One boot sequence and one SCCB wire engine. Runtime ISP changes borrow the
// engine only after AF acknowledges a complete RUN/ERROR transaction boundary.
module camera_config_v02 #(
    parameter integer POWER_DOWN_CYCLES        = 500000,
    parameter integer RESET_ASSERT_END_CYCLES  = 750000,
    parameter integer RESET_RELEASE_END_CYCLES = 1750000,
    parameter integer POST_RESET_DELAY_CYCLES  = 250000,
    parameter integer RETRY_DELAY_CYCLES       = 50000,
    parameter integer ROM_ENTRY_COUNT          = 265,
    parameter INIT_MEM_FILE                    = "ov5640_720p_rgb565.mem",
    parameter integer RUNTIME_MAX_RETRIES       = 3,
    parameter integer SETTLE_FRAME_COUNT        = 3,
    parameter integer SCCB_TIMEOUT_CYCLES       = 100000,
    parameter integer FRAME_TIMEOUT_CYCLES      = 25000000,
    parameter integer AF_WAIT_TIMEOUT_CYCLES    = 1500000000,
    parameter integer AF_ENABLE                = 1,
    parameter integer AF_POLL_CYCLES            = 250000,
    parameter integer AF_MAX_POLLS              = 1001
)(
    input wire clk50, rst, frame_boundary,
    input wire boot_isp, isp_request, isp_apply,
    output reg cam_pwdn, cam_reset_n,
    output wire cam_scl,
    inout wire cam_sda,
    output reg config_done, config_error,
    output reg isp_active, isp_busy, isp_done, isp_error, isp_fatal,
    output reg capture_freeze,
    output wire [15:0] af_status,
    output wire af_error,
    input wire wb_pause_request, wb_start, wb_read,
    input wire [23:0] wb_payload,
    output wire wb_grant, wb_busy, wb_done, wb_ack_error,
    output wire [7:0] wb_read_data
);
    localparam [3:0] POWER=0, BASE=1, SNAPSHOT=2, WRITE_VALUE=3,
        VERIFY_VALUE=4, IDLE=5, WAIT_AF=6, WAIT_FRAME=7, SETTLE=8, FAILED=9;
    reg [3:0] state;
    reg [23:0] rom[0:ROM_ENTRY_COUNT-1];
    reg [7:0] baseline[0:2];
    reg [8:0] index;
    reg [1:0] register_index;
    reg [31:0] power_count, wait_count, delay_count;
    reg [7:0] retry_count, frame_count;
    reg boot_selection, requested_profile, rollback, rollback_failed;
    reg transition_success, inflight, controller_owner, start;
    wire busy, done, ack_error;
    wire [7:0] read_data;
    wire af_start, af_read, af_paused;
    wire [23:0] af_payload;
    // An unconnected WB port preserves legacy instantiations. Ownership is
    // registered only at an AF transaction boundary and retained through done.
    wire wb_requested = (wb_pause_request === 1'b1);
    reg wb_owner;
    wire af_pause_request = isp_busy || isp_fatal || wb_requested || wb_owner;
    wire controller_read = (state == SNAPSHOT || state == VERIFY_VALUE);
    wire [15:0] register_address = (register_index == 0) ? 16'h5580 :
        (register_index == 1) ? 16'h501d : 16'h3406;
    wire [7:0] profile_one = (register_index == 0) ? 8'h06 :
        (register_index == 1) ? 8'h40 : 8'h00;
    wire selected_profile = rollback ? isp_active : requested_profile;
    wire [7:0] selected_value = selected_profile ? profile_one : baseline[register_index];
    wire [23:0] controller_payload = (state == BASE) ? rom[index] :
        {register_address, selected_value};

    initial begin
        if(ROM_ENTRY_COUNT < 2 || ROM_ENTRY_COUNT > 512)
            $error("ROM_ENTRY_COUNT must be between 2 and 512");
        if(SETTLE_FRAME_COUNT < 3 || SETTLE_FRAME_COUNT > 255)
            $error("SETTLE_FRAME_COUNT must be between 3 and 255");
        if(RUNTIME_MAX_RETRIES < 0 || RUNTIME_MAX_RETRIES > 255)
            $error("RUNTIME_MAX_RETRIES must be between 0 and 255");
        $readmemh(INIT_MEM_FILE,rom);
    end
    generate if(AF_ENABLE != 0) begin : gen_af
        ov5640_af #(.POLL_CYCLES(AF_POLL_CYCLES),.MAX_POLLS(AF_MAX_POLLS)) af_i(
            .clk(clk50),.rst(rst),.enable(config_done),
            .pause_request(af_pause_request),.paused(af_paused),
            .start(af_start),.read_enable(af_read),.payload(af_payload),
            .busy(busy),.done(done && !controller_owner && !wb_owner),.ack_error(ack_error),
            .read_data(read_data),.status(af_status),.failed(af_error));
    end else begin : gen_no_af
        assign af_start=0;
        assign af_read=0;
        assign af_payload=0;
        assign af_paused=!busy && !done;
        assign af_status=0;
        assign af_error=0;
    end endgenerate

    sccb_write writer_i(
        .clk(clk50),.rst(rst),
        .start(controller_owner ? start : (wb_owner ? wb_start : af_start)),
        .payload({8'h78,controller_owner ? controller_payload : (wb_owner ? wb_payload : af_payload)}),
        .read_enable(controller_owner ? controller_read : (wb_owner ? wb_read : af_read)),
        .read_data(read_data),.scl(cam_scl),.sda(cam_sda),
        .busy(busy),.done(done),.ack_error(ack_error));

    assign wb_grant=wb_owner;
    assign wb_busy=busy;
    assign wb_done=done && wb_owner;
    assign wb_ack_error=ack_error;
    assign wb_read_data=read_data;
    always @(posedge clk50) begin
        if(rst) wb_owner<=0;
        else if(wb_owner) begin
            if(!wb_requested && !busy && !done && !wb_start) wb_owner<=0;
        end else if(wb_requested && state==IDLE && config_done &&
                    !isp_busy && !isp_fatal && !controller_owner &&
                    af_paused && !busy && !done && !af_start)
            wb_owner<=1;
    end

    // Terminal failure never resets the camera or releases unsafe capture.
    task terminal_failure;
        begin
            isp_error<=1; isp_fatal<=1; isp_busy<=0;
            isp_done<=config_done; capture_freeze<=1;
            if(!config_done) config_error<=1;
            state<=FAILED;
        end
    endtask
    task finish_values;
        begin
            frame_count<=0; wait_count<=0;
            if(rollback_failed) terminal_failure;
            else if(!config_done) begin
                config_done<=1;
                if(!rollback) isp_active<=requested_profile;
                capture_freeze<=0; controller_owner<=0; state<=IDLE;
            end else begin
                transition_success<=!rollback;
                state<=SETTLE;
            end
        end
    endtask
    task transaction_failure;
        begin
            if(retry_count < RUNTIME_MAX_RETRIES) begin
                retry_count<=retry_count+1'b1;
                delay_count<=RETRY_DELAY_CYCLES;
                // Retry a failed readback by rewriting and reading the pair.
                if(state == VERIFY_VALUE) state<=WRITE_VALUE;
            end else begin
                retry_count<=0;
                if(state == BASE || state == SNAPSHOT) terminal_failure;
                else if(!rollback) begin
                    isp_error<=1;
                    if(!config_done) config_error<=1;
                    rollback<=1; rollback_failed<=0;
                    register_index<=0; state<=WRITE_VALUE;
                end else begin
                    // Try the entire previous profile even if one restore fails.
                    rollback_failed<=1;
                    if(register_index==2) terminal_failure;
                    else begin register_index<=register_index+1'b1; state<=WRITE_VALUE; end
                end
            end
        end
    endtask

    always @(posedge clk50) begin
        if(rst) begin
            cam_pwdn<=1; cam_reset_n<=0;
            config_done<=0; config_error<=0;
            isp_active<=0; isp_busy<=0; isp_done<=0; isp_error<=0; isp_fatal<=0;
            capture_freeze<=1; state<=POWER;
            index<=0; register_index<=0; power_count<=0; wait_count<=0; delay_count<=0;
            retry_count<=0; frame_count<=0; boot_selection<=0; requested_profile<=0;
            rollback<=0; rollback_failed<=0; transition_success<=0;
            inflight<=0; controller_owner<=1; start<=0;
            baseline[0]<=0; baseline[1]<=0; baseline[2]<=0;
        end else begin
            start<=0; isp_done<=0;
            case(state)
                POWER: begin
                    if(power_count < POWER_DOWN_CYCLES) begin
                        power_count<=power_count+1'b1; cam_pwdn<=1; cam_reset_n<=0;
                    end else if(power_count < RESET_ASSERT_END_CYCLES) begin
                        power_count<=power_count+1'b1; cam_pwdn<=0; cam_reset_n<=0;
                    end else if(power_count < RESET_RELEASE_END_CYCLES) begin
                        power_count<=power_count+1'b1; cam_pwdn<=0; cam_reset_n<=1;
                    end else begin
                        // Allow the board-switch synchronizer to settle after reset.
                        boot_selection<=boot_isp; state<=BASE;
                    end
                end
                BASE,SNAPSHOT,WRITE_VALUE,VERIFY_VALUE: begin
                    if(delay_count!=0) delay_count<=delay_count-1'b1;
                    else if(!inflight) begin
                        if(!busy && !done) begin
                            start<=1; inflight<=1; wait_count<=0;
                        end else if(wait_count == SCCB_TIMEOUT_CYCLES-1) terminal_failure;
                        else wait_count<=wait_count+1'b1;
                    end else if(done) begin
                        inflight<=0; wait_count<=0;
                        if(ack_error || (state==VERIFY_VALUE && read_data!=selected_value))
                            transaction_failure;
                        else case(state)
                            BASE: begin
                                retry_count<=0;
                                if(index == ROM_ENTRY_COUNT-1) begin
                                    register_index<=0; state<=SNAPSHOT;
                                end else begin
                                    index<=index+1'b1;
                                    if(index==1) delay_count<=POST_RESET_DELAY_CYCLES;
                                end
                            end
                            SNAPSHOT: begin
                                baseline[register_index]<=read_data; retry_count<=0;
                                if(register_index==2) begin
                                    register_index<=0;
                                    if(boot_selection) begin requested_profile<=1; state<=WRITE_VALUE; end
                                    else begin
                                        config_done<=1; capture_freeze<=0;
                                        controller_owner<=0; state<=IDLE;
                                    end
                                end else register_index<=register_index+1'b1;
                            end
                            WRITE_VALUE: state<=VERIFY_VALUE;
                            VERIFY_VALUE: begin
                                retry_count<=0;
                                if(register_index==2) finish_values;
                                else begin register_index<=register_index+1'b1; state<=WRITE_VALUE; end
                            end
                        endcase
                    end else if(wait_count == SCCB_TIMEOUT_CYCLES-1) terminal_failure;
                    else wait_count<=wait_count+1'b1;
                end
                IDLE: begin
                    if(isp_apply && !wb_requested && !wb_owner) begin
                        requested_profile<=isp_request; isp_error<=0;
                        isp_busy<=1; capture_freeze<=1;
                        rollback<=0; rollback_failed<=0; transition_success<=0;
                        wait_count<=0; frame_count<=0; register_index<=0; retry_count<=0;
                        state<=WAIT_AF;
                    end
                end
                WAIT_AF: begin
                    if(af_paused) begin
                        controller_owner<=1; wait_count<=0; frame_count<=0; state<=WAIT_FRAME;
                    end else if(wait_count == AF_WAIT_TIMEOUT_CYCLES-1) terminal_failure;
                    else wait_count<=wait_count+1'b1;
                end
                WAIT_FRAME: begin
                    // Two future boundaries also cover the freeze CDC latency.
                    if(frame_boundary) begin
                        wait_count<=0;
                        if(frame_count==1) begin frame_count<=0; state<=WRITE_VALUE; end
                        else frame_count<=frame_count+1'b1;
                    end else if(wait_count == FRAME_TIMEOUT_CYCLES-1) terminal_failure;
                    else wait_count<=wait_count+1'b1;
                end
                SETTLE: begin
                    if(frame_boundary) begin
                        wait_count<=0;
                        if(frame_count==SETTLE_FRAME_COUNT-1) begin
                            if(transition_success) isp_active<=requested_profile;
                            isp_busy<=0; isp_done<=1; capture_freeze<=0;
                            controller_owner<=0; state<=IDLE;
                        end else frame_count<=frame_count+1'b1;
                    end else if(wait_count == FRAME_TIMEOUT_CYCLES-1) terminal_failure;
                    else wait_count<=wait_count+1'b1;
                end
                FAILED: begin end
                default: terminal_failure;
            endcase
        end
    end
endmodule
