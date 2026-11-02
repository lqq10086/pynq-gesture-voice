"""Preserved native CPU camera color path; never accesses camera hardware."""
import hashlib
import importlib.util
import os
from pathlib import Path

import numpy as np

from native_helpers.v40_color import ColorTransform
from native_helpers.v40_native_ps import NativePS

SOURCE_FILES = (
    'camera_color.py', 'native_helpers/v40_color.py', 'native_helpers/v40_native_ps.py',
    'native/v40_ps.cpp', 'native/libv40_ps.so', 'backend_v51/frame_color.py',
    'backend_v51/native/frame_color.cpp', 'backend_v51/native/libframe_color.so',
    'backend_v51/runtime/frame_reader.py',
) + tuple('hardware_dual/hardware/rtl/' + name for name in (
    'fpga_color_pipeline.v', 'fpga_color_profile.vh',
    'fpga_color_gamma_r.mem', 'fpga_color_gamma_g.mem', 'fpga_color_gamma_b.mem'))


def bound_files(root):
    return [Path(root) / name for name in SOURCE_FILES]


class CameraColor:
    def __init__(self, root):
        self.root = Path(root)
        self.native_ps = self.frame_color = None
        # Desktop numeric tests use the exact integer LUT. Board runtime must
        # retain both qualified native passes; silently falling back costs latency.
        if os.name != 'nt':
            self.native_ps = NativePS(self.root / 'native/libv40_ps.so')
            path = self.root / 'backend_v51/frame_color.py'
            spec = importlib.util.spec_from_file_location('_fastest_frame_color', path)
            self.frame_color = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.frame_color)
            if not self.frame_color.native_used:
                raise RuntimeError('Native RGB565 unpack/mirror kernel is required on board')
        self.transform = ColorTransform(self.root / 'hardware_dual', native_ps=self.native_ps)

    def raw565_to_model_bgr(self, raw, width=1280, height=720):
        source = np.asarray(raw)
        if (source.dtype not in (np.dtype('uint8'), np.dtype('<u2'))
                or not source.flags.c_contiguous or source.nbytes != width * height * 2):
            raise ValueError('Expected private contiguous RGB565 snapshot')
        if self.native_ps is not None:
            image = self.frame_color.raw565_to_mirrored_bgr(source, width, height)
            return self.transform(image)
        words = source.view('<u2').reshape(height, width)
        return np.ascontiguousarray(self.transform.bgr_lut[words[:, ::-1]])

    def identity(self):
        return dict(implementation='NATIVE_UNPACK_MIRROR_THEN_NATIVE_V40_CCM_GAMMA_LUT',
                    execution='NATIVE_CPU' if self.native_ps else 'DESKTOP_EXACT_LUT_REFERENCE',
                    mirror_count=1, color=dict(self.transform.identity),
                    source_sha256={str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in bound_files(self.root)})
