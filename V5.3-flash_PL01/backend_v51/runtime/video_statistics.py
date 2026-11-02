"""V3.3 frozen camera/memory-domain observations; never writes VDMA state.

Offsets are from hardware/VIDEO_STATS_ABI.md, in camera_control_0's HWH range.
Each source domain freezes its counters before acknowledging the request.
The bank stays frozen while software reads it; sequence checks detect another
requester. Cross-domain snapshot instants are bounded, not simultaneous.
"""
import threading
import time
import math

MAGIC = 0x56333353
ABI = 1
FIELDS = {
    'request_axi_cycles': (0x54, 64), 'completed_axi_cycles': (0x5c, 64),
    'pclk_cycles': (0x64, 64), 'vsync_count': (0x6c, 32),
    'complete_frames': (0x70, 32), 'dvp_drop_events': (0x74, 32),
    'fifo_overflow_events': (0x78, 32), 'dvp_axis_beats': (0x7c, 32),
    'dvp_axis_sof': (0x80, 32), 'dvp_axis_eol': (0x84, 32),
    'dvp_axis_errors': (0x88, 32), 'dvp_stall_cycles': (0x8c, 32),
    'mem_cycles': (0x90, 64), 's2mm_axis_beats': (0x98, 32),
    's2mm_axis_sof': (0x9c, 32), 's2mm_axis_eol': (0xa0, 32),
    's2mm_axis_errors': (0xa4, 32), 's2mm_aw_bursts': (0xa8, 32),
    's2mm_b_responses': (0xac, 32), 's2mm_complete_frames': (0xb0, 32),
    's2mm_axi_errors': (0xb4, 32), 'observer_fault': (0xb8, 32),
    'pending_bursts': (0xbc, 32), 'pclk_reset_epoch': (0xc0, 32),
    'mem_reset_epoch': (0xc4, 32), 'vsync_period_pclk_cycles': (0xc8, 32),
    'complete_period_pclk_cycles': (0xcc, 32), 'period_valid': (0xd0, 32),
}


class VideoStatistics:
    def __init__(self, control_mmio):
        self.mmio = control_mmio
        self.lock = threading.Lock()
        if self.mmio.read(0x40) != MAGIC or self.mmio.read(0x44) != ABI:
            raise ValueError('Loaded hardware has no V3.3 coherent statistics ABI')

    def snapshot(self, timeout=.25):
        with self.lock:
            begin = time.monotonic()
            if self.mmio.read(0x4c) & 0x41:
                raise RuntimeError('Statistics snapshot is busy or a source handshake is outstanding')
            previous = self.mmio.read(0x50)
            self.mmio.write(0x48, 1)
            while True:
                status = self.mmio.read(0x4c)
                sequence = self.mmio.read(0x50)
                if status & 0x20:
                    raise TimeoutError('Hardware statistics handshake timed out; status=0x%x' % status)
                if not status & 1 and sequence != previous:
                    if sequence != (previous + 1) & 0xffffffff:
                        raise RuntimeError('Statistics sequence advanced more than one request')
                    break
                if time.monotonic() - begin >= timeout:
                    raise TimeoutError('Statistics source clock/handshake did not complete')
                time.sleep(.0001)
            result = dict(sequence=sequence, status=status,
                          valid=bool(status & 2) and not bool(status & 0x7d),
                          host_request_monotonic=begin)
            for name, (offset, bits) in FIELDS.items():
                value = self.mmio.read(offset)
                if bits == 64: value |= self.mmio.read(offset+4) << 32
                result[name] = value
            after = self.mmio.read(0x4c)
            if self.mmio.read(0x50) != sequence or after != status or after & 1:
                raise RuntimeError('Statistics bank changed during software read')
            result['host_completed_monotonic'] = time.monotonic()
            if not result['valid'] or result['observer_fault']:
                raise ValueError('Statistics source reset or observer fault: %r' % result)
            return result


def snapshot_rates(first, last, axi_clock_hz):
    """Short-window rates; callers must avoid more than one counter wrap.

    AXI reference frequency must come from checked SLCR, not host time or an
    assumed PYNQ clock accessor. Handshake width supplies timing uncertainty.
    """
    if not first['valid'] or not last['valid'] or first['observer_fault'] or last['observer_fault']:
        raise ValueError('Invalid hardware observation')
    if any(first[k] != last[k] for k in ('pclk_reset_epoch', 'mem_reset_epoch')):
        raise ValueError('Source domain reset inside measurement window')
    ticks = last['request_axi_cycles'] - first['request_axi_cycles']
    if (ticks <= 0 or not math.isfinite(axi_clock_hz) or axi_clock_hz <= 0
            or first['sequence'] == last['sequence']):
        raise ValueError('Non-increasing or duplicate snapshot window')
    if any(sample['completed_axi_cycles'] < sample['request_axi_cycles'] for sample in (first,last)):
        raise ValueError('Snapshot completion precedes request')
    seconds = ticks / axi_clock_hz
    if seconds > 30:
        raise ValueError('Use <=30 second windows to bound 32-bit event-counter wraps')
    def delta(key, bits=32): return (last[key]-first[key]) & ((1 << bits)-1)
    width = max(first['completed_axi_cycles']-first['request_axi_cycles'],
                last['completed_axi_cycles']-last['request_axi_cycles']) / axi_clock_hz
    return dict(window_s=seconds, snapshot_skew_bound_s=width,
                sensor_vsync_hz=delta('vsync_count')/seconds,
                pl_complete_hz=delta('complete_frames')/seconds,
                s2mm_complete_hz=delta('s2mm_complete_frames')/seconds,
                pclk_hz=delta('pclk_cycles', 64)/seconds,
                memory_clock_hz=delta('mem_cycles', 64)/seconds,
                dvp_axis_beat_hz=delta('dvp_axis_beats')/seconds,
                s2mm_axis_beat_hz=delta('s2mm_axis_beats')/seconds,
                dvp_drop_events=delta('dvp_drop_events'),
                fifo_overflow_events=delta('fifo_overflow_events'),
                dvp_axis_errors=delta('dvp_axis_errors'),
                s2mm_axis_errors=delta('s2mm_axis_errors'),
                s2mm_axi_errors=delta('s2mm_axi_errors'),
                vsync_events=delta('vsync_count'),
                pl_complete_frames=delta('complete_frames'),
                s2mm_complete_frames=delta('s2mm_complete_frames'),
                reference='checked SLCR AXI clock; frozen request/acknowledge snapshots',
                xvclk_measurement='NOT_MEASURED', physical_obs='NOT_MEASURED')
