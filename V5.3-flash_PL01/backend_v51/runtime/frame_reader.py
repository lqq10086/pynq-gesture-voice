"""Copy one completed RGB565 VDMA frame without stopping the HDMI path."""
import time

import cv2
import numpy as np

PARK_PTR_REG = 0x28


def completed_index(s2mm_current, stores=3):
    if not 0 <= s2mm_current < stores:
        raise ValueError('VDMA S2MM frame index outside buffer ring')
    return (s2mm_current - 1) % stores


def _make_conversion_lut():
    value = np.arange(256,dtype=np.uint8)
    result = np.empty((256,1,3),np.uint8)
    result[:,0,0] = value | (value >> 5)
    result[:,0,1] = value | (value >> 6)
    result[:,0,2] = value | (value >> 5)
    result.setflags(write=False)
    return result


_RGB565_EXPANSION_LUT = _make_conversion_lut()


def rgb565_to_bgr(raw, width=1280, height=720):
    raw=np.asarray(raw)
    if raw.dtype==np.dtype('<u2'):raw=raw.view(np.uint8)
    packed = np.asarray(raw,dtype=np.uint8).reshape(height,width,2)
    shifted = cv2.cvtColor(packed,cv2.COLOR_BGR5652BGR)
    # OpenCV shifts the source bits; restore the baseline's low-bit replication.
    return cv2.LUT(shifted,_RGB565_EXPANSION_LUT)


class CompletedFrameReader:
    def __init__(self, camera, max_copy_s=.025, convert_bgr=False):
        self.camera = camera
        self.last_pointer = None
        self.last_transition = None
        self.period = None
        self.frame_id = 0
        # Conservative copy budget for the fixed 720P camera mode. This is a
        # safety limit, not a measured frame period or a capture FPS claim.
        self.max_copy_s = max_copy_s
        self.convert_bgr = convert_bgr

    def _pointer(self):
        # WrFrmStore is the working S2MM buffer on this AXI VDMA instance.
        value = self.camera.vdma_mmio.read(PARK_PTR_REG)
        return (value >> 24) & 31

    def read(self, timeout=1.0):
        read_started=time.monotonic()
        if not self.camera.video_running or len(self.camera.buffers) != 3:
            raise RuntimeError('Camera video buffer ring is not running')
        deadline = time.monotonic()+timeout
        # Inference skips an unknown number of ring wraps. Wait for only one
        # fresh transition; an old period cannot measure this sampling window.
        self.last_pointer = self._pointer()
        self.last_transition = None
        self.period = None
        while True:
            pointer = self._pointer()
            now = time.monotonic()
            completed_index(pointer)
            if pointer != self.last_pointer:
                self.last_pointer = pointer
                self.last_transition = now
                self.frame_id += 1
                break
            if now >= deadline:
                raise TimeoutError('No new OV5640 S2MM frame pointer')
            time.sleep(.001)
        index = completed_index(pointer)
        source = self.camera.buffers[index]
        start = time.monotonic()
        source.invalidate()
        raw = np.asarray(source).copy()
        copied = time.monotonic()
        after = self._pointer()
        if after == index or copied-start >= self.max_copy_s:
            raise RuntimeError('Frame copy exceeded stable VDMA buffer window')
        conversion_start=time.monotonic()
        bgr=rgb565_to_bgr(raw) if self.convert_bgr else None
        conversion_end=time.monotonic()
        result={'frame_id':self.frame_id, 'raw565':raw,
                'source_frame_id':self.frame_id,
                'source_frame_complete_monotonic':now,
                'frame_complete_monotonic':now, 'copy_ms':(copied-start)*1000,
                'wait_transition_ms':(now-read_started)*1000,
                'copy_guard_ms':(conversion_start-copied)*1000,
                'rgb565_to_bgr_ms':(conversion_end-conversion_start)*1000 if self.convert_bgr else 0.,
                'frame_read_total_ms':(conversion_end-read_started)*1000,
                'period_ms':None, 'capture_fps_status':'NOT_MEASURED_SINGLE_TRANSITION',
                'buffer_index':index}
        if self.convert_bgr:result['bgr']=bgr
        return result
