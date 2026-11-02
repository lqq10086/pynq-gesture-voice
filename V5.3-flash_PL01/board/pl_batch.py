"""ctypes binding to the one-call native batch, with a generated register map."""
import ctypes as C
import hashlib
import math
from pathlib import Path
import numpy as np
from register_map import ARGUMENTS


class NativeArgument(C.Structure):_fields_=[('low',C.c_uint32),('high',C.c_uint32),('width',C.c_uint32)]
class NativeMap(C.Structure):
    _fields_=[('control',C.c_uint32),('return_offset',C.c_uint32),('start_mask',C.c_uint32),('done_mask',C.c_uint32),('idle_mask',C.c_uint32),('auto_restart_mask',C.c_uint32),('arguments',NativeArgument*19)]
class NativeOperation(C.Structure):_fields_=[('values',C.c_uint64*19)]
class NativeResult(C.Structure):
    _fields_=[('code',C.c_int32),('completed_ops',C.c_uint32),('failed_op',C.c_uint32),('last_control',C.c_uint32),('accelerator_return',C.c_int32),('reserved',C.c_uint32),('elapsed_ns',C.c_uint64)]


class NativeBatch:
    def __init__(self,library,register_map):
        self.library_sha256=hashlib.sha256(Path(library).read_bytes()).hexdigest()
        self.library=C.CDLL(str(library));self.map=register_map;m=NativeMap()
        for name,key in [('control','control_offset'),('return_offset','return_offset'),('start_mask','start_mask'),('done_mask','done_mask'),('idle_mask','idle_mask'),('auto_restart_mask','auto_restart_mask')]:setattr(m,name,register_map[key])
        for i,name in enumerate(ARGUMENTS):
            arg=register_map['arguments'][name];m.arguments[i]=NativeArgument(arg['low_offset'],0xffffffff if arg['high_offset'] is None else arg['high_offset'],arg['width_bits'])
        self.native_map=m;self.library.pl_batch_run.argtypes=[C.POINTER(C.c_uint32),C.c_uint32,C.POINTER(NativeMap),C.POINTER(NativeOperation),C.c_uint32,C.c_uint64,C.POINTER(NativeResult)]
        self.library.pl_batch_run.restype=C.c_int

    def run(self,registers,operations,timeout_s):
        if not math.isfinite(timeout_s) or timeout_s<=0 or timeout_s*1e9>=2**64:raise ValueError('finite positive monotonic batch timeout required')
        registers=np.asarray(registers)
        if registers.dtype!=np.uint32 or not registers.flags.c_contiguous or not registers.flags.writeable:raise ValueError('writable contiguous uint32 MMIO array required')
        rows=operations if isinstance(operations,C.Array) and operations._type_ is NativeOperation else prepare_operations(operations)
        result=NativeResult();self.library.pl_batch_run(registers.ctypes.data_as(C.POINTER(C.c_uint32)),registers.nbytes,C.byref(self.native_map),rows,len(rows),int(timeout_s*1e9),C.byref(result))
        return result


def prepare_operations(operations):
    rows=(NativeOperation*len(operations))()
    for index,op in enumerate(operations):
        for j,name in enumerate(ARGUMENTS):
            value=int(op[name])
            if value<0 or value>=2**64:raise ValueError('unsigned register argument out of range')
            rows[index].values[j]=value
    return rows
