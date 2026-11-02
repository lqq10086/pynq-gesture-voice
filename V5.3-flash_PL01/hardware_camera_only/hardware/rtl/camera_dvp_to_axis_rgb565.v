`timescale 1ns/1ps

module camera_dvp_to_axis_rgb565 #(
    parameter integer ACTIVE_WIDTH        = 1280,
    parameter integer ACTIVE_HEIGHT       = 720,
    parameter integer STARTUP_DROP_FRAMES = 10,
    parameter integer FIFO_DEPTH          = 256
) (
    input  wire        pclk,
    input  wire        rst,
    input  wire        capture_enable,
    input  wire        vsync,
    input  wire        href,
    input  wire [7:0]  data,

    output wire [15:0] m_axis_tdata,
    output wire [1:0]  m_axis_tkeep,
    output wire        m_axis_tvalid,
    input  wire        m_axis_tready,
    output wire        m_axis_tuser,
    output wire        m_axis_tlast,

    output reg         frame_start,
    output reg         frame_done,
    output reg         frame_seen,
    output reg         fifo_overflow,
    output reg         addr_overflow,
    output reg  [$clog2(ACTIVE_WIDTH * ACTIVE_HEIGHT + 1)-1:0] accepted_pixel_count,
    output reg  [15:0] complete_frame_count,
    output reg  [15:0] dropped_frame_count,
    output wire [1:0] drop_events,
    output wire overflow_event
);
    localparam integer PTR_WIDTH   = $clog2(FIFO_DEPTH);
    localparam integer COUNT_WIDTH = $clog2(FIFO_DEPTH + 1);
    localparam integer FRAME_PIXELS      = ACTIVE_WIDTH * ACTIVE_HEIGHT;
    localparam integer X_COUNT_WIDTH     = $clog2(ACTIVE_WIDTH + 1);
    localparam integer Y_COUNT_WIDTH     = $clog2(ACTIVE_HEIGHT + 1);
    localparam integer FRAME_COUNT_WIDTH = $clog2(FRAME_PIXELS + 1);

    reg [17:0] fifo [0:FIFO_DEPTH-1];
    reg [PTR_WIDTH-1:0] write_ptr;
    reg [PTR_WIDTH-1:0] read_ptr;
    reg [COUNT_WIDTH-1:0] fifo_count;

    reg        byte_phase;
    reg [7:0]  first_byte;
    reg        vsync_d;
    reg        href_d;
    reg [X_COUNT_WIDTH-1:0] x_count;
    reg [Y_COUNT_WIDTH-1:0] y_count;
    reg [FRAME_COUNT_WIDTH-1:0] line_base;
    reg [7:0]  startup_frames;
    reg        capture_active;
    reg        frame_good;
    reg        frame_overflow;

    // Isolate the source-synchronous DVP bus from the parser. These registers
    // stay in fabric because Zynq-7000 IOB packing inserts a large ZHOLD delay.
    (* IOB = "FALSE" *) reg [7:0] camera_data;
    (* IOB = "FALSE" *) reg       camera_href;
    (* IOB = "FALSE" *) reg       camera_vsync;

    always @(posedge pclk) begin
        camera_data  <= data;
        camera_href  <= href;
        camera_vsync <= vsync;
    end

    wire axis_pop = m_axis_tvalid && m_axis_tready;
    wire pixel_complete = camera_href && href_d && capture_active && byte_phase &&
                          (x_count < ACTIVE_WIDTH) && (y_count < ACTIVE_HEIGHT);
    wire fifo_space = (fifo_count < FIFO_DEPTH) || axis_pop;
    wire fifo_push = pixel_complete && fifo_space;
    wire [FRAME_COUNT_WIDTH-1:0] pixel_address = line_base + x_count;

    assign m_axis_tvalid = (fifo_count != 0);
    assign m_axis_tdata  = fifo[read_ptr][15:0];
    assign m_axis_tuser  = fifo[read_ptr][16];
    assign m_axis_tlast  = fifo[read_ptr][17];
    assign m_axis_tkeep  = 2'b11;

    // Include startup/disabled/nonempty rejection and accepted-but-bad frames.
    // One VSYNC can abandon the old frame and reject the new: two events.
    wire sensor_edge = camera_vsync && !vsync_d;
    wire reject_new = (startup_frames < STARTUP_DROP_FRAMES) || !capture_enable || (fifo_count != 0);
    wire bad_end = !sensor_edge && !camera_href && href_d && capture_active &&
        (y_count == ACTIVE_HEIGHT-1) &&
        (!frame_good || x_count != ACTIVE_WIDTH || byte_phase || frame_overflow || addr_overflow);
    assign drop_events = rst ? 2'd0 : (sensor_edge ?
        ({1'b0,capture_active}+{1'b0,reject_new}) : {1'b0,bad_end});
    assign overflow_event = !rst && pixel_complete && !fifo_space;


    // Register 0x4740 is programmed to 0x21. The sensor changes DVP data on
    // PCLK's falling edge; the external input constraint preserves that
    // cycle alignment. V3.3 samples with a +90-degree delayed PCLK, giving
    // three quarters of a source period for setup and one quarter for hold.
    always @(posedge pclk) begin
        if (rst) begin
            write_ptr             <= 0;
            read_ptr              <= 0;
            fifo_count            <= 0;
            byte_phase            <= 1'b0;
            first_byte            <= 8'd0;
            vsync_d               <= 1'b0;
            href_d                <= 1'b0;
            x_count               <= {X_COUNT_WIDTH{1'b0}};
            y_count               <= {Y_COUNT_WIDTH{1'b0}};
            line_base             <= {FRAME_COUNT_WIDTH{1'b0}};
            startup_frames        <= 8'd0;
            capture_active        <= 1'b0;
            frame_good            <= 1'b0;
            frame_overflow        <= 1'b0;
            frame_start           <= 1'b0;
            frame_done            <= 1'b0;
            frame_seen            <= 1'b0;
            fifo_overflow         <= 1'b0;
            addr_overflow         <= 1'b0;
            accepted_pixel_count  <= {FRAME_COUNT_WIDTH{1'b0}};
            complete_frame_count  <= 16'd0;
            dropped_frame_count   <= 16'd0;
        end else begin
            vsync_d    <= camera_vsync;
            href_d     <= camera_href;
            frame_start <= 1'b0;
            frame_done  <= 1'b0;

            if (axis_pop)
                read_ptr <= read_ptr + 1'b1;

            if (fifo_push) begin
                fifo[write_ptr] <= {
                    (x_count == ACTIVE_WIDTH-1),
                    ((x_count == 0) && (y_count == 0)),
                    first_byte,
                    camera_data
                };
                write_ptr <= write_ptr + 1'b1;
            end else if (pixel_complete) begin
                fifo_overflow <= 1'b1;
                frame_overflow <= 1'b1;
                frame_good    <= 1'b0;
            end

            case ({fifo_push, axis_pop})
                2'b10: fifo_count <= fifo_count + 1'b1;
                2'b01: fifo_count <= fifo_count - 1'b1;
                default: fifo_count <= fifo_count;
            endcase

            if (camera_vsync && !vsync_d) begin
                frame_start          <= 1'b1;
                byte_phase           <= 1'b0;
                x_count              <= {X_COUNT_WIDTH{1'b0}};
                y_count              <= {Y_COUNT_WIDTH{1'b0}};
                line_base            <= {FRAME_COUNT_WIDTH{1'b0}};

                if (capture_active) begin
                    dropped_frame_count <= dropped_frame_count + 1'b1;
                    frame_good          <= 1'b0;
                end

                if (startup_frames < STARTUP_DROP_FRAMES) begin
                    startup_frames <= startup_frames + 1'b1;
                    capture_active <= 1'b0;
                    frame_good     <= 1'b0;
                // capture_enable is a synchronized level. Keeping it high
                // captures consecutive complete sensor frames. The lens
                // pipeline may deassert it to protect RAW ownership, while
                // the color-profile controller independently gates it during
                // frame-safe SCCB reconfiguration.
                end else if (capture_enable && (fifo_count == 0)) begin
                    capture_active <= 1'b1;
                    frame_good     <= 1'b1;
                    accepted_pixel_count <= {FRAME_COUNT_WIDTH{1'b0}};
                    addr_overflow  <= 1'b0;
                    frame_overflow <= 1'b0;
                end else begin
                    capture_active <= 1'b0;
                    frame_good     <= 1'b0;
                end
            end else if (camera_href && capture_active) begin
                if (!href_d) begin
                    // HREF is the byte-phase reference for every line.
                    x_count    <= {X_COUNT_WIDTH{1'b0}};
                    first_byte <= camera_data;
                    byte_phase <= 1'b1;
                end else if (!byte_phase) begin
                    if (x_count >= ACTIVE_WIDTH) begin
                        frame_good <= 1'b0;
                        addr_overflow <= 1'b1;
                    end
                    first_byte <= camera_data;
                    byte_phase <= 1'b1;
                end else begin
                    byte_phase <= 1'b0;
                    if (x_count < ACTIVE_WIDTH) begin
                        if (pixel_address >= FRAME_PIXELS) begin
                            addr_overflow <= 1'b1;
                            frame_good    <= 1'b0;
                        end else if (fifo_push) begin
                            accepted_pixel_count <= accepted_pixel_count + 1'b1;
                        end
                        x_count <= x_count + 1'b1;
                    end
                end
            end else begin
                byte_phase <= 1'b0;
                if (href_d && capture_active) begin
                    if ((x_count != ACTIVE_WIDTH) || byte_phase)
                        frame_good <= 1'b0;

                    if (y_count < ACTIVE_HEIGHT-1) begin
                        y_count   <= y_count + 1'b1;
                        line_base <= line_base + ACTIVE_WIDTH;
                    end else begin
                        capture_active <= 1'b0;
                        if (frame_good && (x_count == ACTIVE_WIDTH) && !byte_phase &&
                            !frame_overflow && !addr_overflow) begin
                            frame_done           <= 1'b1;
                            frame_seen           <= 1'b1;
                            complete_frame_count <= complete_frame_count + 1'b1;
                        end else begin
                            dropped_frame_count <= dropped_frame_count + 1'b1;
                        end
                    end
                end
            end
        end
    end
endmodule
