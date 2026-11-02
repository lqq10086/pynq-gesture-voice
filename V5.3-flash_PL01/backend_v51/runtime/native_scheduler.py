"""External per-instance _invoke candidate. Never creates MMIO or owns buffers.

Install only after the unchanged qualified RegionExecutor has initialized.
Keep the original owner lock, run(), wait_idle(), and safe-release lifecycle.
"""
import ctypes
import hashlib
import math
import mmap
from pathlib import Path
import sys
import threading

import numpy as np

EXECUTOR_SHA256 = '93cc6db8bc2a2c7030ad8565a8a2adacad7ef48222d2704d21fee8a97b51ddb2'


class _Result(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ('writes', 'polls', 'sleeps', 'status', 'last_control', 'started')]


def _load_library(path):
    library = ctypes.CDLL(str(Path(path).resolve()))  # CDLL releases the GIL.
    library.host_scheduler_abi.argtypes = []
    library.host_scheduler_abi.restype = ctypes.c_uint32
    if library.host_scheduler_abi() != 1:
        raise RuntimeError('Unexpected native scheduler ABI')
    library.invoke = library.host_scheduler_invoke
    library.invoke.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t, ctypes.c_double, ctypes.c_uint32, ctypes.POINTER(_Result)]
    library.invoke.restype = ctypes.c_int
    return library


def _region_module(executor):
    # Both existing import styles (region_executor / runtime.region_executor)
    # are supported without importing a second production module instance.
    module = sys.modules[type(executor).__module__]
    if type(executor) is not getattr(module, 'RegionExecutor', None):
        raise RuntimeError('Expected the existing qualified RegionExecutor')
    if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != EXECUTOR_SHA256:
        raise RuntimeError('RegionExecutor changed; candidate requires review')
    return module


def _mapped_array(mmio):
    from pynq import MMIO
    if (type(mmio) is not MMIO
            or not mmio.device.has_capability('MEMORY_MAPPED')
            or getattr(mmio.read, '__func__', None) is not MMIO.read
            or getattr(mmio.write, '__func__', None) is not MMIO.write_mm):
        raise RuntimeError('Only original PYNQ MEMORY_MAPPED MMIO is supported')
    array = mmio.array
    if (type(array) is not np.ndarray or array.dtype != np.dtype('<u4')
            or array.ndim != 1 or not array.flags.c_contiguous
            or not array.flags.writeable or not array.flags.aligned
            or mmio.base_addr != 0x43c00000 or mmio.length != 0x10000
            or array.nbytes != mmio.length):
        raise RuntimeError('Unexpected existing accelerator MMIO mapping')
    backing = array.base
    if isinstance(backing, memoryview):
        backing = backing.obj
    if not isinstance(backing, mmap.mmap):
        raise RuntimeError('MMIO ndarray must retain its original mmap backing')
    return array


class _Invocation:
    def __init__(self, executor, library, array):
        self.executor, self.library, self.array = executor, library, array
        self.module = _region_module(executor)
        self.original = executor._invoke
        self.last_result = None
        self.lock = threading.Lock()

    def __call__(self, index, diagnostic=False, timeout=20):
        owner = self.executor
        if not owner.buffers or owner.poisoned:
            raise RuntimeError('Closed/failed executor; owned buffers retained until verified idle')
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Native diagnostic timeout must be finite and positive')
        if owner.regs.array is not self.array:
            raise RuntimeError('Existing MMIO array changed')
        if not self.lock.acquire(False):
            raise RuntimeError('Concurrent native invocation refused')
        try:
            d = owner.descriptors[index]
            second = (int(owner.weight.physical_address) + d['weight_padded_offset']
                if d['op_type'].startswith('Conv') else int(owner.tensors[
                    d['sources'][1] if len(d['sources']) > 1 else d['sources'][0]].physical_address))
            pointers = [int(owner.tensors[d['sources'][0]].physical_address), second,
                int(owner.bias.physical_address) + d.get('bias_offset', 0),
                int(owner.requant.physical_address) + d.get('quant_parameter_offset', 0),
                int(owner.acc.physical_address), int(owner.tensors[d['output_tensor']].physical_address)]
            cache = getattr(owner, '_config_cache', None)
            if cache is None:
                owner._config_cache = cache = {}
                owner._config_write_count = owner._config_write_changed_count = 0
            changes = [(offset, int(value), cache.get(offset) is not None)
                for offset, value in zip(self.module.POINTER_OFFSETS + self.module.SCALAR_OFFSETS,
                                         pointers + self.module.register_values(d, diagnostic))
                if cache.get(offset) != int(value)]
            offsets = (ctypes.c_uint32 * len(changes))(*(row[0] for row in changes))
            values = (ctypes.c_uint32 * len(changes))(*(row[1] & 0xffffffff for row in changes))
            result = _Result()
            try:
                code = self.library.invoke(self.array.ctypes.data, self.array.size,
                    offsets, values, len(changes), timeout, 100000, ctypes.byref(result))
            finally:
                # The original cache is updated even when start times out or
                # PL returns an error: these registers were already written.
                for offset, value, changed in changes[:result.writes]:
                    cache[offset] = value
                    owner._config_write_count += 1
                    owner._config_write_changed_count += int(changed)
                self.last_result = {name: int(getattr(result, name)) for name, _ in result._fields_}
            if code == 1:
                raise TimeoutError('PL operation %d timed out; all CMA retained' % index)
            if code == 2:
                raise RuntimeError('PL rejected operation %d with status %d' % (index, result.status))
            if code:
                raise RuntimeError('Native scheduler error %d; all CMA retained' % code)
        except BaseException:
            owner.poisoned = True
            raise
        finally:
            self.lock.release()

    def restore(self):
        """Restore only the method after original idle proof; never free CMA."""
        self.executor.wait_idle()
        if self.executor._invoke is not self:
            raise RuntimeError('Executor invocation changed since installation')
        self.executor._invoke = self.original


def install(executor, library_path):
    """Opt-in for an already-owned, initialized Linux board diagnostic run."""
    if sys.platform != 'linux':
        raise RuntimeError('Hardware installation requires the existing Linux board owner')
    module = _region_module(executor)
    if (not getattr(executor, 'qualification', None) or not executor.buffers
            or executor.poisoned or getattr(executor._invoke, '__func__', None) is not module.RegionExecutor._invoke):
        raise RuntimeError('Expected a healthy qualified executor with its original _invoke')
    array = _mapped_array(executor.regs)
    library = _load_library(library_path)
    executor.wait_idle()
    candidate = _Invocation(executor, library, array)
    executor._invoke = candidate
    return candidate
