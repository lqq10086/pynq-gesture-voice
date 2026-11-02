`timescale 1ns/1ps

module tb_axis_mirror_rgb565;
    localparam integer WIDTH = 8;
    reg clk = 0;
    always #5 clk = ~clk;
    reg rst = 1;
    reg [15:0] s_axis_tdata = 0;
    reg s_axis_tvalid = 0, s_axis_tuser = 0, s_axis_tlast = 0;
    wire s_axis_tready;
    wire [15:0] m_axis_tdata;
    wire m_axis_tvalid, m_axis_tuser, m_axis_tlast;
    reg m_axis_tready = 0;
    wire sync_error;

    axis_mirror_rgb565 #(.WIDTH(WIDTH)) dut (.*);

    // A reference line is collected only from accepted input transfers.
    // Expected pixels are enqueued in reverse order, independently of DUT state.
    reg [15:0] input_line [0:WIDTH-1];
    integer expected_data[$], expected_user[$], expected_last[$];
    integer in_x = 0, sent = 0, received = 0, verified = 0;
    integer want_data, want_user, want_last, k;
    reg line_sof = 0, expected_error = 0;
    reg held = 0;
    reg [17:0] held_payload = 0;
    integer sink_mode = 0;
    integer source_seed = 32'h479a201b, sink_seed = 32'h18a55c91;
    integer unused_random;
    reg [31:0] sink_random;
    reg throughput_check = 0, first_output = 0;
    integer throughput_target = 0;
    integer cases_passed = 0;

    always @(negedge clk) begin
        sink_random = $random(sink_seed);
        if (rst || sink_mode == 2)
            m_axis_tready = 0;
        else if (sink_mode == 1)
            m_axis_tready = sink_random[3:0] < 10;
        else
            m_axis_tready = 1;
    end

    always @(posedge clk) begin
        if (rst) begin
            expected_data.delete();
            expected_user.delete();
            expected_last.delete();
            in_x = 0; sent = 0; received = 0;
            expected_error = 0; line_sof = 0;
            held = 0; first_output = 0;
            #1;
            if (m_axis_tvalid !== 0 || sync_error !== 0)
                $fatal(1, "Reset did not flush output/error state");
        end else begin
            if (held && (m_axis_tvalid !== 1 ||
                {m_axis_tuser,m_axis_tlast,m_axis_tdata} !== held_payload))
                $fatal(1, "Output changed during backpressure");
            held = m_axis_tvalid && !m_axis_tready;
            if (held)
                held_payload = {m_axis_tuser,m_axis_tlast,m_axis_tdata};

            if (s_axis_tvalid && s_axis_tready) begin
                input_line[in_x] = s_axis_tdata;
                if (in_x == 0) line_sof = s_axis_tuser;
                if (s_axis_tlast != (in_x == WIDTH-1) ||
                    (s_axis_tuser && in_x != 0)) expected_error = 1;
                sent = sent + 1;
                if (in_x == WIDTH-1) begin
                    for (k = WIDTH-1; k >= 0; k = k-1) begin
                        expected_data.push_back(input_line[k]);
                        expected_user.push_back(line_sof && k == WIDTH-1);
                        expected_last.push_back(k == 0);
                    end
                    in_x = 0;
                end else in_x = in_x + 1;
            end
            if (throughput_check && first_output &&
                received < throughput_target && m_axis_tvalid !== 1)
                $fatal(1, "Bubble in continuous one-pixel-per-cycle output");
            if (m_axis_tvalid && m_axis_tready) begin
                if (expected_data.size() == 0)
                    $fatal(1, "Unexpected output before a complete input line or after reset");
                want_data = expected_data.pop_front();
                want_user = expected_user.pop_front();
                want_last = expected_last.pop_front();
                if (m_axis_tdata !== want_data[15:0] ||
                    m_axis_tuser !== want_user[0] || m_axis_tlast !== want_last[0])
                    $fatal(1, "Pixel %0d got %h/%b/%b expected %h/%b/%b",
                        received, m_axis_tdata, m_axis_tuser, m_axis_tlast,
                        want_data[15:0], want_user[0], want_last[0]);
                received = received + 1;
                verified = verified + 1;
                first_output = 1;
            end
            #1;
            if (sync_error !== expected_error)
                $fatal(1, "sync_error=%b expected=%b", sync_error, expected_error);
        end
    end

    task automatic reset_stream;
        begin
            @(negedge clk);
            rst = 1; s_axis_tvalid = 0;
            throughput_check = 0;
            repeat (3) @(posedge clk);
            @(negedge clk); rst = 0;
        end
    endtask

    task automatic send_pixel(input [15:0] data, input bit user_bit,
                              input bit last_bit, input integer gaps);
        integer gap;
        begin
            for (gap = 0; gap < gaps; gap = gap+1) begin
                @(negedge clk); s_axis_tvalid = 0;
            end
            @(negedge clk);
            s_axis_tdata = data; s_axis_tuser = user_bit;
            s_axis_tlast = last_bit; s_axis_tvalid = 1;
            @(posedge clk);
            while (!s_axis_tready) @(posedge clk);
        end
    endtask

    task automatic send_frames(input integer frames, input integer lines,
                               input integer tag, input bit random_gaps);
        integer frame, line, pixel, gaps;
        reg [31:0] random_value;
        begin
            for (frame = 0; frame < frames; frame = frame+1)
                for (line = 0; line < lines; line = line+1)
                    for (pixel = 0; pixel < WIDTH; pixel = pixel+1) begin
                        random_value = $random(source_seed);
                        gaps = random_gaps ? random_value[1:0] : 0;
                        send_pixel(tag + frame*256 + line*16 + pixel,
                                   line == 0 && pixel == 0,
                                   pixel == WIDTH-1, gaps);
                    end
            @(negedge clk); s_axis_tvalid = 0;
        end
    endtask

    task automatic drain(input string label_text);
        integer cycles;
        begin
            cycles = 0;
            while (received != sent) begin
                @(negedge clk);
                cycles = cycles+1;
                if (cycles > 20000) $fatal(1, "Drain timeout in %s", label_text);
            end
            repeat (4) @(negedge clk);
            if (expected_data.size() != 0 || in_x != 0 || m_axis_tvalid !== 0)
                $fatal(1, "Unflushed data in %s", label_text);
            cases_passed = cases_passed+1;
            $display("CASE_PASS %s pixels=%0d", label_text, received);
        end
    endtask

    task automatic malformed_case(input integer kind);
        integer line, pixel;
        bit user_bit, last_bit;
        begin
            reset_stream(); sink_mode = 1;
            for (line = 0; line < 4; line = line+1)
                for (pixel = 0; pixel < WIDTH; pixel = pixel+1) begin
                    user_bit = line == 0 && pixel == 0;
                    last_bit = pixel == WIDTH-1;
                    if (kind == 1 && line == 0 && pixel == 2) last_bit = 1;
                    if (kind == 2 && line == 0 && pixel == WIDTH-1) last_bit = 0;
                    if (kind == 2 && line == 1 && pixel == 0) last_bit = 1;
                    if (kind == 3 && line == 0 && pixel == 3) user_bit = 1;
                    send_pixel(16'h7000 + kind*256 + line*16 + pixel,
                               user_bit, last_bit, 0);
                end
            @(negedge clk); s_axis_tvalid = 0;
            drain($sformatf("malformed_%0d_no_forced_realign", kind));
            repeat (10) @(negedge clk);
            if (sync_error !== 1) $fatal(1, "Error was not sticky");
        end
    endtask

    integer pixel;
    initial begin
        if ($value$plusargs("SEED=%d", source_seed))
            sink_seed = source_seed ^ 32'h5a913bc7;
        $display("MIRROR_TEST_START source_seed=%0d sink_seed=%0d", source_seed, sink_seed);
        reset_stream(); sink_mode = 0;
        throughput_check = 1; throughput_target = 3*5*WIDTH;
        send_frames(3, 5, 16'h1000, 0);
        drain("continuous_frames_no_bubbles");
        throughput_check = 0;

        reset_stream(); sink_mode = 1;
        send_frames(20, 5, 16'h2000, 1);
        drain("random_source_gaps_and_sink_backpressure");

        reset_stream(); sink_mode = 2;
        send_frames(1, 2, 16'h4000, 0);
        @(negedge clk);
        s_axis_tvalid = 1; s_axis_tdata = 16'hdead;
        s_axis_tuser = 1; s_axis_tlast = 0;
        repeat (12) begin
            @(posedge clk);
            if (s_axis_tready !== 0 || m_axis_tvalid !== 1)
                $fatal(1, "Full banks did not backpressure with a stalled output");
        end
        reset_stream(); sink_mode = 1;
        send_frames(3, 3, 16'h5000, 1);
        drain("reset_flushes_full_banks_and_stalled_output");

        reset_stream(); sink_mode = 0;
        for (pixel = 0; pixel < 3; pixel = pixel+1)
            send_pixel(16'hdead + pixel, pixel == 0, 0, 0);
        reset_stream(); sink_mode = 1;
        send_frames(3, 2, 16'h6000, 1);
        drain("reset_flushes_partial_line");

        malformed_case(1);
        malformed_case(2);
        malformed_case(3);
        reset_stream(); sink_mode = 0;
        send_frames(1, 2, 16'h8000, 0);
        drain("reset_clears_sticky_error");
        if (sync_error !== 0) $fatal(1, "Reset did not clear sticky error");
        $display("AXIS_MIRROR_RGB565_PASS cases=%0d verified_pixels=%0d", cases_passed, verified);
        $finish;
    end

    initial begin
        #2000000;
        $fatal(1, "Global simulation timeout");
    end
endmodule
