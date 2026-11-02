"""Four corrected samples per output pixel; one mirror; native BGR stretch384."""
import ctypes
import os
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent


def axis(source, target=384, mirror=False, horizontal=True):
    coordinate = ((np.arange(target, dtype=np.float64) + .5) * (source / target) - .5).astype(np.float32)
    index = np.floor(coordinate).astype(np.int32)
    fraction = (coordinate - index).astype(np.float32)
    if horizontal:
        fraction[(index < 0) | (index >= source - 1)] = 0
    weights = np.stack((np.rint((np.float32(1) - fraction) * np.float32(2048)),
                        np.rint(fraction * np.float32(2048))), axis=1).astype(np.int16)
    indices = np.clip(np.stack((index, index + 1), axis=1), 0, source - 1)
    if mirror:
        indices = source - 1 - indices
    return np.ascontiguousarray(indices, dtype=np.int32), np.ascontiguousarray(weights)


class FusedPreprocessor:
    """Outputs are reusable scratch buffers; copy them if retained across calls."""
    def __init__(self, color_lut, source_shape=(720, 1280), library=None):
        self.height, self.width = map(int, source_shape)
        if not (1 <= self.width <= 16384 and 1 <= self.height <= 16384):
            raise ValueError('Invalid source dimensions')
        if np.shape(color_lut) != (65536, 3) or np.asarray(color_lut).dtype != np.uint8:
            raise ValueError('Expected exact BGR uint8 CCM/gamma table [65536,3]')
        self.lut = np.ascontiguousarray(color_lut).copy()
        self.lut.setflags(write=False)
        self.xs, self.alpha = axis(self.width, mirror=True)
        self.ys, self.beta = axis(self.height, horizontal=False)
        self.normalized = np.arange(256, dtype=np.float32) / np.float32(255.)
        path = Path(library) if library else HERE / 'build' / ('fused_preprocess.dll' if os.name == 'nt' else 'libfused_preprocess.so')
        if not path.is_file():
            raise FileNotFoundError('Compile native/build_preprocess.py first: ' + str(path))
        self.library_path = path.resolve()
        self.library = ctypes.CDLL(str(path))
        parameters = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                      ctypes.c_void_p, ctypes.c_size_t] + [ctypes.c_void_p] * 5 + [ctypes.c_size_t]
        for name in ('pl01_fused_u8', 'pl01_fused_fp32', 'pl01_fused_i16'):
            function = getattr(self.library, name)
            function.argtypes = parameters + ([] if name.endswith('_u8') else [ctypes.c_void_p])
            function.restype = ctypes.c_int
        self.u8 = np.empty((384, 384, 3), np.uint8)
        self.fp32 = np.empty((1, 3, 384, 384), np.float32)
        self.i16 = np.empty((1, 3, 384, 384), np.int16)
        self.exponent = self.quantized = None

    def _run(self, raw, name, output, values=None):
        source = np.asarray(raw)
        if (source.dtype not in (np.dtype('uint8'), np.dtype('<u2')) or not source.flags.c_contiguous
                or source.nbytes != self.width * self.height * 2):
            raise ValueError('Expected stable contiguous little-endian RGB565 source')
        args = [source.ctypes.data, source.nbytes, self.width, self.height,
                self.lut.ctypes.data, self.lut.nbytes, self.xs.ctypes.data, self.ys.ctypes.data,
                self.alpha.ctypes.data, self.beta.ctypes.data, output.ctypes.data, output.size]
        if values is not None:
            args.append(values.ctypes.data)
        if getattr(self.library, name)(*args):
            raise RuntimeError('Fused native preprocessor rejected its contract')
        return output

    def run_u8(self, raw):
        return self._run(raw, 'pl01_fused_u8', self.u8)

    def run_fp32(self, raw):
        return self._run(raw, 'pl01_fused_fp32', self.fp32, self.normalized)

    def run_i16(self, raw, exponent):
        if int(exponent) != exponent or not -30 <= exponent <= 30:
            raise ValueError('Expected integer activation exponent in [-30,30]')
        if exponent != self.exponent:
            self.quantized = np.clip(np.rint(self.normalized / np.float32(2. ** exponent)), -32768, 32767).astype(np.int16)
            self.exponent = exponent
        return self._run(raw, 'pl01_fused_i16', self.i16, self.quantized)
