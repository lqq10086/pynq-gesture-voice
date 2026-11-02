"""Application runner seam: native packed RGB565 preprocessing -> owned PL."""
import os
from pathlib import Path
import sys
import threading
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(ROOT/'host'))


class PLRunnerAdapter:
    def __init__(self,pl,preprocessor=None,*,root=ROOT,core_guard=None):
        self.pl=pl;self.core_guard=core_guard;self.closed=False;self.hardware_released=False
        self.worker_lock=threading.Lock();self.last_timings=self.last_times=None;self.batches=0;self.pipeline=None;self.release_thread_id=None
        if preprocessor is None:
            from native.fused_preprocess_v2 import PackedPreprocessor
            root=Path(root);lut=np.fromfile(root/'native/ccm_gamma_bgr_lut.bin',np.uint8).reshape(65536,3)
            library=root/'native/build_v2'/('fused_preprocess_v2.dll' if os.name=='nt' else 'armv7/libfused_preprocess_v2.so')
            preprocessor=PackedPreprocessor(lut,library=library,mode='packed_scalar')
        self.preprocessor=preprocessor

    def _forward(self,value):
        if self.closed:raise RuntimeError('PL adapter closed; retained owner must finish shutdown')
        started=time.monotonic();output=self.pl.infer(value);self.batches+=1
        from gesture_app.inference import validate_output
        output=validate_output(output,2)
        self.last_timings=self.last_times=dict(network_ms=(time.monotonic()-started)*1000,
            inference_device='PL',pl_batch_invocations=1,pl_operator_invocations=96)
        return output

    def _infer_quantized(self,value):
        if not self.worker_lock.acquire(False):raise RuntimeError('PL inference already active')
        try:
            return self._forward(value)
        finally:self.worker_lock.release()

    def infer(self,tensor):
        from gesture_app.inference import validate_input
        from integer_reference import quantize
        validate_input(tensor)
        return self._infer_quantized(quantize(tensor,self.pl.input_exponent,16))

    def infer_raw565(self,raw):
        if not self.worker_lock.acquire(False):raise RuntimeError('PL preprocessing/inference already active')
        try:
            if self.closed:raise RuntimeError('PL adapter closed')
            started=time.monotonic();value=self.preprocessor.run_i16(raw,self.pl.input_exponent);prepared=time.monotonic()
            output=self._forward(value)
            from gesture_app.inference import Geometry
            self.last_timings.update(input_path='NATIVE_PACKED_ONE_MIRROR_CCM_GAMMA_BGR_STRETCH384_A16',
                preprocess_ms=(prepared-started)*1000,raw_to_decoded_ms=(time.monotonic()-started)*1000)
            return output,Geometry(1280,720,384/1280,0.,0.,0,0)
        finally:self.worker_lock.release()

    def identity(self):
        return dict(execution_backend='PL_FASTEST_W8PC_A16',decoded_output_shape=[1,2160,7],
            pl_nn_acceleration=True,pl_batch_invocations=self.batches,pl_operator_invocations=self.batches*96,
            input_exponent=self.pl.input_exponent,preprocessor='packed_scalar; one mirror, CCM/gamma before stretch384')

    def close(self):
        self.closed=True
        if not self.worker_lock.acquire(False):return False
        try:self.pl.close();return True
        finally:self.worker_lock.release()

    def release_hardware(self,*,owner_confirmed=False):
        if self.hardware_released:return True
        if not self.closed or not self.worker_lock.acquire(False):return False
        self.release_thread_id=threading.get_ident()
        try:
            if self.core_guard is not None:self.core_guard.check_idle(shutdown=True)
            if not self.pl.release(owner_confirmed=owner_confirmed):return False
            self.hardware_released=True;return True
        finally:self.release_thread_id=None;self.worker_lock.release()
