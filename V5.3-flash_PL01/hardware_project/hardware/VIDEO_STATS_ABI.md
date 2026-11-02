# V3.3 video statistics ABI 1

Base address: `camera_control_0`, `0x41220000`, verified by the generated HWH/address map. Existing color-control registers `0x00..0x14` retain their ABI. New registers are aligned 32-bit words. All counters wrap modulo 2^32 unless explicitly 64-bit. Counters accumulate for the loaded bitstream lifetime; datapath resets increment epochs and reset only protocol state.

Write 1 to `0x48` to request a snapshot; poll `0x4C` until busy clears. A busy/unrecovered transport rejects requests with AXI SLVERR. Read the sequence before and after the payload, require the same sequence and `valid=1`, reject timeouts, and reject rate intervals crossing either reset epoch. Only the device owner should issue requests. No register clears VDMA interrupts, resets DMA, or changes capture ownership.

| Offset | Value |
|---|---|
| 40 | Magic `56333353` (V33S) |
| 44 | ABI version 1 |
| 48 | Write bit 0: request; read zero |
| 4C | Status: bit 0 busy, 1 valid, 2 captured camera reset, 3 captured S2MM reset, 4 S2MM observer fault, 5 transport timeout, 6 recovery pending |
| 50 | Snapshot sequence, incremented only after both transports finish |
| 54,58 | 64-bit AXI clock timestamp when request accepted, low/high |
| 5C,60 | 64-bit AXI clock timestamp when transport completed or timed out, low/high |
| 64,68 | 64-bit camera sampling/PCLK cycles at camera freeze, low/high |
| 6C | Sensor VSYNC rising events, independent of capture enable |
| 70 | DVP complete target frames: exactly 1280 valid RGB565 pixels on each of 720 accepted lines, no partial byte/overflow/line error |
| 74 | DVP drops: startup, capture disabled, nonempty FIFO at frame arm, unfinished previous frame, or accepted malformed frame. One VSYNC can account for two different frames |
| 78 | FIFO overflow pixel events, not frames |
| 7C | DVP-side AXIS accepted beats |
| 80,84 | DVP-side accepted SOF/EOL |
| 88 | DVP-side AXIS format error events |
| 8C | DVP-side valid-without-ready cycles |
| 90,94 | 64-bit memory clock cycles at memory freeze, low/high |
| 98,9C,A0,A4 | S2MM input accepted beats, SOF, EOL, format errors |
| A8,AC | S2MM memory AW burst handshakes, B response handshakes |
| B0 | Confirmed complete packed-frame writes, incremented only on the final clean AXI B response |
| B4 | S2MM observer error cycles |
| B8 | Sticky observer faults: bit 0 address/size/burst/frame geometry, 1 tag FIFO overflow, 2 response without outstanding AW, 3 B error/ID, 4 partial WSTRB |
| BC | Pending AW-to-B burst tags at freeze |
| C0,C4 | Camera reset epoch, S2MM primary reset epoch |
| C8 | Most recent VSYNC interval in PCLK cycles |
| CC | Most recent DVP complete-frame interval in PCLK cycles |
| D0 | Period validity: bit 0 VSYNC interval, bit 1 DVP completion interval |

The period registers are last-event samples. Poll faster than a frame and de-duplicate by the corresponding cumulative event count. A count jump means intermediate intervals were not sampled; do not manufacture missing intervals or report their P95 as a complete distribution. D0 requires two events since the last camera reset.

## Clock-domain consistency and timing window

`stats_snapshot_cdc.v` synchronizes a request toggle into each source clock with XPM, freezes its entire payload in one source edge, and transfers that held payload through `xpm_cdc_handshake`. Transport completion waits for the source handshake to return idle. The measured datapath reset never resets transport, avoiding mismatched toggles after a VDMA reset. Source reset flags/epochs are part of the same atomic payload.

Camera and memory snapshots are coherent **within their individual domains**. They are not frozen on exactly the same edge. Request/completion AXI timestamps bracket both freezes. With a verified AXI FCLK frequency, calculate PCLK rate from camera-cycle deltas over successive request timestamp deltas, and report the transport-window uncertainty. For an interval, a conservative time interval is `[request_B-complete_A, complete_B-request_A]`. Rates derived from a source-domain cycle delta and event delta avoid PS polling jitter; also retain the actual time window and epoch validity. XVCLK is not connected to the FPGA and remains unmeasured without a board-level measurement.

Timeout is 16,777,216 AXI cycles (about 167.8 ms at 100 MHz). Timeout clears busy and valid and sets timeout. Late payloads cannot turn the failed request into a valid sample. Until both transports recover, new requests receive SLVERR. Reloading a bitstream clears all state. Software must treat timeout/recovery as invalid, not wait forever or reuse an old sample.

## S2MM completion boundary

The observer is a passive tap on the actual VDMA M_AXI_S2MM AW, WSTRB and B signals before the HP0 interconnect. This VDMA master has no AXI ID ports, so responses are ordered on its single transaction stream. A 64-entry marker FIFO attaches the end-of-frame marker to the last AW burst in each 1,843,200-byte packed frame. Completion is counted only when that burst's ordered `BVALID && BREADY` response is OKAY and no observer fault has occurred. It is independent of PARKPTR polling, AXIS SOF arrival, fsync, or sticky IRQ levels.

Required runtime geometry: 1280x720 RGB565, 2560-byte packed stride, 8-byte aligned frame starts, INCR bursts with 8-byte beats, all WSTRB bytes asserted. Address continuity is checked within every packed frame; the next frame may use a different buffer base. Any mismatch, missing/overflowed tag, response error or partial strobe stops confirmed-completion counting for that epoch. The actual VDMA `s2mm_prmry_reset_out_n` resets protocol tracking; cumulative counts remain and the reset epoch advances. Intervals with error/fault/epoch changes are invalid for an acceptance claim.

This proves ordered AXI write completion for the packed frame, not sensor exposure time, PS cache ownership, display refresh, or OBS visibility. Continue the existing cache invalidation/copy guards and DDR/CMA lifecycle protections. Sensor/AXIS errors are separately required to remain absent: a clean memory write alone does not certify a correctly shaped camera frame.

## Verification

XSIM test sources: `tests/video_stats_tb.sv` and `tests/camera_dvp_720p30_tb.sv`. Logs under `reports/hardware/rtl_sim` cover source-domain coherent freezing, halted-clock timeout/recovery, reset epochs, period validity, final-B completion, B errors, malformed bursts, tag overflow, orphan B, address discontinuity, partial strobes, correct/incorrect AXIS geometry, DVP odd/overlong/short lines and overflow recovery. Routed timing, CDC and board measurements are separate gates.
