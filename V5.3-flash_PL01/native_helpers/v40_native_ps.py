"""Explicitly loaded CPU kernels; no MMIO, allocation of CMA, or PL fallback."""
import ctypes
import hashlib
from pathlib import Path

import numpy as np


class NativePS:
    INPUT_SHAPE = (1, 3, 384, 384)

    def __init__(self, library_path):
        path = Path(library_path).resolve(strict=True)
        self.library = ctypes.CDLL(str(path))
        self._validate = self.library.v40_validate_fp32
        self._validate.argtypes = (ctypes.c_void_p, ctypes.c_size_t)
        self._validate.restype = ctypes.c_int
        self._quantize = self.library.v40_quantize_grid
        self._quantize.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                  ctypes.c_void_p, ctypes.c_float,
                                  ctypes.c_void_p, ctypes.c_size_t)
        self._quantize.restype = ctypes.c_int
        self._pack = self.library.v40_letterbox_rgb_chw
        self._pack.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t,
                              ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t,
                              ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
        self._pack.restype = ctypes.c_int
        self._color = self.library.v40_color_lookup
        self._color.argtypes = (ctypes.c_void_p, ctypes.c_size_t) * 3
        self._color.restype = ctypes.c_int
        self._normalized = np.arange(256, dtype=np.float32) / np.float32(255.)
        self._normalized.setflags(write=False)
        self._quantized = self._scale = None
        source = Path(__file__).resolve().parents[1] / "native/v40_ps.cpp"
        self.identity = {
            "implementation": "V40_NATIVE_PS_EXACT_PACK_GRID_AND_RGB565_LUT",
            "preprocessing": "UNCHANGED_OPENCV_RESIZE_EXACT_FP32_U8_DIV255_PACK",
            "quantization": "BIT_EQUAL_U8_GRID_LUT_OTHER_FP32_STRICT_DIVIDE_RINT_CLIP",
            "library_path": str(path),
            "library_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        }

    @classmethod
    def _check_input(cls, value):
        if (not isinstance(value, np.ndarray) or value.dtype != np.float32
                or value.shape != cls.INPUT_SHAPE or not value.flags.c_contiguous):
            raise ValueError("Expected contiguous FP32 [1,3,384,384]")
        # Contiguous NumPy views may be unaligned; ARM float loads require alignment.
        return value if value.flags.aligned else np.require(value, requirements=["A"])

    @staticmethod
    def _require_success(status):
        if status == 0:
            raise ValueError("Expected finite RGB FP32 values in [0,1]")
        if status != 1:
            raise RuntimeError("Native PS rejected arguments/FP mode: %d" % status)

    def validate(self, value):
        source = self._check_input(value)
        self._require_success(self._validate(source.ctypes.data, source.size))

    def quantize(self, value, scale, output):
        source = self._check_input(value)
        if (not isinstance(output, np.ndarray) or output.dtype != np.int16
                or output.shape != self.INPUT_SHAPE or not output.flags.c_contiguous
                or not output.flags.writeable or np.shares_memory(value, output)):
            raise ValueError("Expected separate writable contiguous int16 output")
        target = output if output.flags.aligned else np.empty(output.shape, np.int16)
        scale = np.float32(scale)
        if not np.isfinite(scale) or scale <= 0:
            raise ValueError("Expected a finite positive input scale")
        if scale != self._scale:
            quantized = np.divide(self._normalized, scale)
            np.rint(quantized, out=quantized)
            np.clip(quantized, -32767, 32767, out=quantized)
            self._quantized = quantized.astype(np.int16)
            self._quantized.setflags(write=False)
            self._scale = scale
        self._require_success(self._quantize(source.ctypes.data, source.size,
                                            self._normalized.ctypes.data,
                                            self._quantized.ctypes.data, scale,
                                            target.ctypes.data, target.size))
        if target is not output:
            np.copyto(output, target)
        return output

    def pack_letterbox(self, value, top, left):
        if (not isinstance(value, np.ndarray) or value.dtype != np.uint8
                or value.ndim != 3 or value.shape[2] != 3
                or min(value.shape[:2]) < 1 or max(value.shape[:2]) > 384
                or not isinstance(top, int) or not isinstance(left, int)
                or not 0 <= top <= 384 - value.shape[0]
                or not 0 <= left <= 384 - value.shape[1]):
            raise ValueError("Expected resized uint8 BGR within 384 letterbox")
        source = np.ascontiguousarray(value)
        output = np.empty(self.INPUT_SHAPE, np.float32)
        self._require_success(self._pack(source.ctypes.data, source.nbytes,
                                        source.shape[0], source.shape[1], top, left,
                                        self._normalized.ctypes.data,
                                        output.ctypes.data, output.size))
        return output

    def color(self, value, lut):
        if (not isinstance(value, np.ndarray) or value.dtype != np.uint8
                or value.ndim != 3 or value.shape[2] != 3):
            raise ValueError("Expected clean uint8 BGR camera frame")
        if (lut.dtype != np.uint8 or lut.shape != (65536, 3)
                or not lut.flags.c_contiguous):
            raise ValueError("Expected contiguous uint8 BGR LUT [65536,3]")
        # Preserve the reference transform's support for strided image views.
        source = np.ascontiguousarray(value)
        output = np.empty(source.shape, np.uint8)
        if source.size:
            self._require_success(self._color(source.ctypes.data, source.size // 3,
                                             lut.ctypes.data, lut.nbytes,
                                             output.ctypes.data, output.nbytes))
        return output
