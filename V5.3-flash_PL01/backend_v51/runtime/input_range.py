"""Read-only symmetric-range check with the original NumPy fallback."""
import ctypes
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LIBRARY_PATH = ROOT / 'native/libinput_range.so'
_library = None
_validate_native = None
native_load_error = None
if sys.platform.startswith('linux') and LIBRARY_PATH.exists():
    try:
        _library = ctypes.CDLL(str(LIBRARY_PATH))
        _validate_native = _library.validate_symmetric_input
        _validate_native.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
        _validate_native.restype = ctypes.c_int
    except (OSError, AttributeError) as error:
        native_load_error = str(error)
        _library = _validate_native = None
native_used = _validate_native is not None


def validate_symmetric_input(value):
    if (not isinstance(value, np.ndarray) or value.dtype not in (np.dtype(np.int8), np.dtype(np.int16))
            or not value.flags.c_contiguous):
        raise ValueError('Expected contiguous signed int8 or int16 input')
    if _validate_native is None:
        if np.any(value == np.iinfo(value.dtype).min):
            raise ValueError('Input outside symmetric activation range')
        return
    result = int(_validate_native(value.ctypes.data, value.size, value.dtype.itemsize * 8))
    if result == 0:
        raise ValueError('Input outside symmetric activation range')
    if result != 1:
        raise RuntimeError('Native input range validator returned %d' % result)
