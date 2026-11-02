"""Start independent parked RGB565 pages before the first MM2S stream.

Construct from CameraControlSession's output_factory after S2MM priming.
Interrupting an already running MM2S stream latches the existing PL HDMI
underflow telemetry until the pixel-domain reset, so this driver rejects it.
The camera owns all DMA shutdown.

Register fields are from AMD AXI VDMA v6.3 PG020 and the qualified HWH:
https://docs.amd.com/r/en-US/pg020_axi_vdma/MM2S_VDMACR-MM2S-VDMA-Control-Register-Offset-00h
https://docs.amd.com/r/en-US/pg020_axi_vdma/PARK_PTR_REG-Park-Pointer-Register-Offset-28h
"""
from pathlib import Path
import threading
import time
import xml.etree.ElementTree as ET

import numpy as np
from frame_color import bgr_to_rgb565,mirror_rgb565,native_used,native_rgb565_used

WIDTH, HEIGHT, STRIDE, FRAME_BYTES = 1280, 720, 2560, 1843200
MM2S_CONTROL, MM2S_STATUS, PARK_PTR = 0x00, 0x04, 0x28
S2MM_CONTROL, S2MM_STATUS = 0x30, 0x34
VSIZE, HSIZE, FRAME_STRIDE, START_ADDRESS = 0x50, 0x54, 0x58, 0x5c
ERROR_BITS = 0x770
PARKED_CONTROL = 1 << 16  # One-frame interrupt threshold, no frame-count stop.


class HdmiOutput:
    def __init__(self, camera):
        self.camera = camera
        self.regs = camera.vdma_mmio
        self.buffers = []
        self.timeout_s = 2.0
        self._closed = False
        self._requested = 0
        self._lock = threading.Lock()
        if self.regs is None or len(camera.buffers) != 3:
            raise RuntimeError('HDMI output requires the allocated qualified camera ring')
        if self.regs.read(MM2S_CONTROL) & 1 or not self.regs.read(MM2S_STATUS) & 1:
            raise RuntimeError('Configure HDMI output before the first MM2S start')
        if not self.regs.read(S2MM_CONTROL) & 1 or self.regs.read(S2MM_STATUS) & 1:
            raise RuntimeError('Prime the camera S2MM ring before starting HDMI output')
        previous = getattr(camera, 'hdmi_output', None)
        if previous is not None and not previous._closed:
            raise RuntimeError('Camera already owns independent HDMI output buffers')
        self._validate_layout()
        # Retain strong ownership even if allocation, retargeting or startup fails.
        # The caller then halts the camera and closes camera.hdmi_output.
        camera.hdmi_output = self
        intervals = [(int(b.device_address), int(b.device_address) + int(b.nbytes))
                     for b in camera.buffers]
        for _ in range(3):
            buffer = camera._allocate_one()
            self.buffers.append(buffer)
            address, end = int(buffer.device_address), int(buffer.device_address) + int(buffer.nbytes)
            if (buffer.nbytes != FRAME_BYTES or address % 8 or
                    not 0 <= address < end <= 0x40000000):
                raise RuntimeError('HDMI CMA buffer is unaligned or outside qualified HP0 DDR')
            if any(address < old_end and end > old_address for old_address, old_end in intervals):
                raise RuntimeError('HDMI CMA buffer overlaps another owned camera/output buffer')
            intervals.append((address, end))
            buffer[:] = 0
            buffer.flush()
        self.regs.write(MM2S_CONTROL, PARKED_CONTROL)
        self.regs.write(HSIZE, STRIDE)
        self.regs.write(FRAME_STRIDE, STRIDE)
        for index, buffer in enumerate(self.buffers):
            offset = START_ADDRESS + index * 4
            self.regs.write(offset, int(buffer.device_address))
            if self.regs.read(offset) != int(buffer.device_address):
                raise RuntimeError('MM2S output address readback failed')
        self._request_page(0)
        self.regs.write(MM2S_CONTROL, PARKED_CONTROL | 1)
        self.regs.write(VSIZE, HEIGHT)  # PG020: VSIZE commits the channel parameters.
        self._wait_current(0)

    def _validate_layout(self):
        entry = self.camera.overlay.ip_dict.get('axi_vdma_0', {})
        if (int(entry.get('phys_addr', -1)) != 0x43000000 or
                entry.get('type') != 'xilinx.com:ip:axi_vdma:6.3'):
            raise RuntimeError('HDMI requires the qualified AXI VDMA v6.3 mapping')
        bit_path = self.camera.bit_path or self.camera.overlay.bitfile_name
        root = ET.parse(Path(bit_path).with_suffix('.hwh')).getroot()
        module = next((m for m in root.findall('.//MODULE')
                       if m.get('INSTANCE') == 'axi_vdma_0'), None)
        if module is None:
            raise RuntimeError('HWH has no axi_vdma_0')
        parameters = {p.get('NAME'): p.get('VALUE')
                      for p in module.findall('./PARAMETERS/PARAMETER')}
        expected = {'C_NUM_FSTORES': '3', 'C_INCLUDE_MM2S': '1',
                    'C_INCLUDE_S2MM': '1', 'C_INCLUDE_SG': '0',
                    'C_M_AXI_MM2S_ADDR_WIDTH': '32', 'C_M_AXIS_MM2S_TDATA_WIDTH': '16',
                    'C_ENABLE_VIDPRMTR_READS': '1', 'C_MM2S_GENLOCK_MODE': '3'}
        if any(parameters.get(name) != value for name, value in expected.items()):
            raise RuntimeError('HWH differs from qualified RGB565 triple-ring video layout')

    def _wait_current(self, index):
        deadline = time.monotonic() + self.timeout_s
        while True:
            status = int(self.regs.read(MM2S_STATUS))
            if status & ERROR_BITS:
                raise RuntimeError('MM2S output error: 0x%x' % (status & ERROR_BITS))
            current = (int(self.regs.read(PARK_PTR)) >> 16) & 31
            if not status & 1 and current == index:
                return current
            if time.monotonic() >= deadline:
                raise TimeoutError('MM2S page switch was not acknowledged; output buffers retained')
            time.sleep(.001)

    def _request_page(self, index):
        # Preserve S2MM's writable reference bits; the frame-store status is RO.
        value = int(self.regs.read(PARK_PTR))
        self.regs.write(PARK_PTR, (value & ~31) | index)
        self._requested = index

    def write_frame(self, bgr):
        if not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8 or bgr.shape != (HEIGHT, WIDTH, 3):
            raise ValueError('HDMI expects a uint8 1280x720 BGR frame')
        return self._write_frame(bgr, raw=False)

    def write_rgb565(self, raw565, mirror=True):
        """Display an owned PS snapshot; never retarget MM2S to the capture ring."""
        source = np.asarray(raw565)
        if (source.dtype not in (np.dtype(np.uint8), np.dtype('<u2'))
                or source.nbytes != FRAME_BYTES or not source.flags.c_contiguous):
            raise ValueError('HDMI expects contiguous 1280x720 RGB565')
        if any(np.shares_memory(source, buffer) for buffer in self.camera.buffers + self.buffers):
            raise ValueError('RGB565 input must be a private PS snapshot, not an owned DMA page')
        return self._write_frame(source, raw=True, mirror=mirror)

    def _write_frame(self, source, raw, mirror=False):
        with self._lock:
            if self._closed:
                raise RuntimeError('HDMI output is closed')
            started = time.monotonic()
            # A previous timeout may leave a pending switch. Do not touch any
            # output buffer until its requested page has actually become current.
            current = self._wait_current(self._requested)
            target = (current + 1) % 3
            destination = np.asarray(self.buffers[target]).reshape(HEIGHT, WIDTH, 2)
            converted_start = time.monotonic()
            if raw:
                if mirror:
                    mirror_rgb565(source, destination=destination)
                else:
                    np.copyto(destination, source.view(np.uint8).reshape(HEIGHT, WIDTH, 2))
            else:
                bgr_to_rgb565(source,destination=destination)
            converted = time.monotonic()
            self.buffers[target].flush()
            flushed = time.monotonic()
            self._request_page(target)
            self._wait_current(target)
            acknowledged = time.monotonic()
            return {'display_index': target, 'previous_display_index': current,
                    'input_format': 'RGB565' if raw else 'BGR',
                    'rgb565_stage': ('direct_mirror' if mirror else 'direct_copy') if raw else 'bgr_pack',
                    'rgb565_convert_ms': (converted - converted_start) * 1000,
                    'output_flush_ms': (flushed - converted) * 1000,
                    'park_ack_ms': (acknowledged - flushed) * 1000,
                    'hdmi_write_ms': (acknowledged - started) * 1000,
                    'display_ack_monotonic': acknowledged,
                    'native_color_conversion': native_rgb565_used if raw else native_used,
                    'ack_semantics': 'MM2S current frame-store pointer; not OBS capture timing'}

    def close(self):
        with self._lock:
            if self._closed:
                return
            controls = self.regs.read(MM2S_CONTROL) | self.regs.read(S2MM_CONTROL)
            halted = (self.regs.read(MM2S_STATUS) & 1 and self.regs.read(S2MM_STATUS) & 1)
            if self.camera.video_running or controls & 1 or not halted:
                raise RuntimeError('Halt both camera DMA channels before releasing HDMI buffers')
            for buffer in self.buffers:
                buffer.freebuffer()
            self.buffers = []
            self._closed = True
