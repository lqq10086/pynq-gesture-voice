"""A16 sigmoid tables with the retained FP32 Detect arithmetic."""
import numpy as np
import threading
import ctypes
import os
from pathlib import Path

_library_path = Path(__file__).resolve().parents[1] / 'native' / ('quantized_decode.dll' if os.name == 'nt' else 'libquantized_decode.so')
_native_decode_head = None
if _library_path.exists():
    _library = ctypes.CDLL(str(_library_path))
    _native_abi = getattr(_library,'decode_head_abi',None)
    if _native_abi is None:raise RuntimeError('Native Detect decoder lacks the two-class ABI7 identity')
    _native_abi.argtypes=[];_native_abi.restype=ctypes.c_int
    if _native_abi()!=7:raise RuntimeError('Native Detect decoder ABI differs from the two-class seven-column contract')
    _native_decode_head = _library.decode_head
    _native_decode_head.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_int, ctypes.c_float, ctypes.c_void_p]
    _native_decode_head.restype = ctypes.c_int

try:
    from .pipeline import IndependentDecoder
except ImportError:
    from pipeline import IndependentDecoder


class QuantizedDecoder(IndependentDecoder):
    """Accept the three ordered CHW int16 executor heads of a validated package."""
    def __init__(self, package):
        manifest = package['manifest']
        detect = manifest['detect']
        boundaries = package['boundaries']
        if manifest['classes'] != ['open_palm', 'pinch_point']:
            raise ValueError('Two-class order changed')
        if detect['strides'] != [8., 16., 32.] or detect['channels_per_anchor'] != 7:
            raise ValueError('Unexpected Detect strides/channels')
        anchors = np.asarray(detect['anchors_pixels'], np.float32)
        if anchors.shape != (3, 3, 2) or not np.isfinite(anchors).all() or (anchors <= 0).any():
            raise ValueError('Expected finite positive Detect anchors')
        if len(boundaries) != 3:
            raise ValueError('Expected three A16 head boundaries')
        self.scales = []
        for index, (boundary, h) in enumerate(zip(boundaries, (48, 24, 12))):
            scale = np.float32(boundary['scale'])
            if (boundary['name'] != '/model.24/m.%d/Conv_output_0' % index
                    or boundary['shape'] != [1, 21, h, h] or boundary['bits'] != 16
                    or not np.isfinite(scale) or scale <= 0):
                raise ValueError('Unexpected A16 head shape/order/scale')
            self.scales.append(scale)
        super().__init__(detect)
        self._output=self.output
        del self.output
        self._decode_lock=threading.Lock()
        self.tables = []
        # Index by the bit representation, avoiding a signed offset on each frame.
        codes = np.arange(65536, dtype=np.uint16).view(np.int16)
        for scale in self.scales:
            table = np.multiply(codes, scale, dtype=np.float32)
            # Exact current FP32 operation order, with no clipping or approximation.
            np.negative(table, out=table)
            np.exp(table, out=table)
            np.add(table, 1, out=table)
            np.reciprocal(table, out=table)
            if not np.isfinite(table).all():
                raise ValueError('Nonfinite sigmoid table')
            table.setflags(write=False)
            self.tables.append(table)

    def decode(self, heads):
        # Public callers own the returned snapshot; no alias to the next decode.
        return self._decode_and_consume(heads,lambda output:output.copy())

    def _decode_and_consume(self,heads,consume):
        """Private immediate consumer holds the reusable output until completion."""
        with self._decode_lock:
            output=self._decode(heads)
            output.setflags(write=False)
            try:return consume(output)
            finally:output.setflags(write=True)

    def _decode(self, heads):
        if len(heads) != 3:
            raise ValueError('Expected three ordered CHW int16 heads')
        offset = 0
        for raw, table, stride, (work, grid, anchors), h in zip(
                heads, self.tables, self.strides, self.buffers, (48, 24, 12)):
            if not isinstance(raw, np.ndarray) or raw.shape != (21, h, h) or raw.dtype != np.int16:
                raise ValueError('Unexpected A16 raw head shape/dtype')
            if _native_decode_head is not None and raw.flags.c_contiguous:
                status = _native_decode_head(raw.ctypes.data, table.ctypes.data, anchors.ctypes.data,
                                             h, stride, self._output.ctypes.data + offset * 7 * 4)
                if status:
                    raise RuntimeError('Native Detect decode failed: %d' % status)
                offset += 3 * h * h
                continue
            indices = raw.view(np.uint16).reshape(1, 3, 7, h, h).transpose(0, 1, 3, 4, 2)
            # Every uint16 index is in bounds; clip avoids np.take's raise-mode copy.
            np.take(table, indices, out=work, mode='clip')
            np.multiply(work[..., :2], 2, out=work[..., :2])
            np.add(work[..., :2], grid, out=work[..., :2])
            np.multiply(work[..., :2], stride, out=work[..., :2])
            np.multiply(work[..., 2:4], 2, out=work[..., 2:4])
            np.square(work[..., 2:4], out=work[..., 2:4])
            np.multiply(work[..., 2:4], anchors, out=work[..., 2:4])
            count = 3 * h * h
            self._output[:, offset:offset + count] = work.reshape(1, count, 7)
            offset += count
        if self._output.shape != (1, 9072, 7) or self._output.dtype != np.float32 or not np.isfinite(self._output).all():
            raise ValueError('Expected finite FP32 decoded [1,9072,7]')
        return self._output
