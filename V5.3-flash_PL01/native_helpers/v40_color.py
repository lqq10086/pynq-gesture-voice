"""Board-local replica of the retained post-DDR integer CCM/gamma RTL.

Only clean camera pixels enter this transform. HDMI still applies the actual PL
stage to the pre-correction UI/background once. UVC/MJPEG parity is unmeasured.
"""
import hashlib
import re
from pathlib import Path
import numpy as np


def round_shift(value, bits):
    magnitude = (np.abs(value) + (1 << (bits - 1))) >> bits
    return np.where(value < 0, -magnitude, magnitude)


class ColorTransform:
    def __init__(self, root, *, native_ps=None):
        self.native_ps = native_ps
        rtl = Path(root) / "hardware/rtl"
        profile = (rtl / "fpga_color_profile.vh").read_text()
        constants = {name: int(value) for name, value in
                     re.findall(r"`define FPGA_COLOR_(\w+) (-?\d+)", profile)}
        words = np.arange(65536, dtype=np.int64)
        r, g, b = words >> 11, (words >> 5) & 63, words & 31
        rgb = np.stack(((r << 7) | (r << 2) | (r >> 3), (g << 6) | g,
                        (b << 7) | (b << 2) | (b >> 3)), axis=1)
        gains = np.array([constants["GAIN_%s_Q12" % c] for c in "RGB"])
        offsets = np.array([constants["OFFSET_%s" % c] for c in "RGB"])
        rgb = round_shift(rgb * gains, 12) + offsets
        ccm = np.array([[constants["CCM_%s%s_Q12" % (row, col)]
                         for col in "RGB"] for row in "RGB"])
        rgb = round_shift(rgb @ ccm.T, 12)
        luma = round_shift(rgb @ np.array([77, 150, 29]), 8)[:, None]
        rgb = luma + round_shift((rgb - luma) * constants["SATURATION_Q12"], 12)
        rgb = np.clip(rgb, 0, 4095)
        files = [rtl / "fpga_color_pipeline.v", rtl / "fpga_color_profile.vh"]
        for i, channel in enumerate("rgb"):
            path = rtl / ("fpga_color_gamma_%s.mem" % channel)
            lut = np.array([int(v, 16) for v in path.read_text().split()], np.int64)
            if lut.shape != (4096,) or np.any(lut < 0) or np.any(lut > 4095):
                raise ValueError("Invalid 12-bit gamma LUT")
            rgb[:, i] = lut[rgb[:, i]]
            files.append(path)
        packed = (rgb * np.array([31, 63, 31]) + 2048) >> 12
        self.word_lut = ((packed[:, 0] << 11) | (packed[:, 1] << 5) |
                         packed[:, 2]).astype(np.uint16)
        self.bgr_lut = self.expand_bgr(self.word_lut)
        self.word_lut.setflags(write=False)
        self.bgr_lut.setflags(write=False)
        self.identity = {
            "implementation": "65536_ENTRY_EXACT_RGB565_RTL_INTEGER_LUT",
            "mirror": "caller_already_mirrored_once",
            "uvc_mjpeg_parity": "NOT_TESTED",
            "physical_hdmi_parity": "NOT_TESTED",
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        }
        if native_ps is not None:
            self.identity["native_ps"] = dict(native_ps.identity)

    @staticmethod
    def expand_bgr(words):
        r, g, b = words >> 11, (words >> 5) & 63, words & 31
        return np.stack(((b << 3) | (b >> 2), (g << 2) | (g >> 4),
                         (r << 3) | (r >> 2)), axis=-1).astype(np.uint8)

    def __call__(self, bgr):
        if bgr.dtype != np.uint8 or bgr.ndim != 3 or bgr.shape[2] != 3:
            raise ValueError("Expected clean uint8 BGR camera frame")
        if self.native_ps is not None:
            return self.native_ps.color(bgr, self.bgr_lut)
        words = ((bgr[:, :, 2].astype(np.uint16) >> 3) << 11)
        words |= (bgr[:, :, 1].astype(np.uint16) >> 2) << 5
        words |= bgr[:, :, 0].astype(np.uint16) >> 3
        return self.bgr_lut[words]
