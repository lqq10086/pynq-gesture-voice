"""Packed BGR0 LUT / ARM NEON candidate; retained scalar API stays available."""
import ctypes
import os
from pathlib import Path
import numpy as np

try:
    from .fused_preprocess import FusedPreprocessor
except ImportError:
    from fused_preprocess import FusedPreprocessor


class PackedPreprocessor(FusedPreprocessor):
    def __init__(self,color_lut,source_shape=(720,1280),library=None,mode='neon'):
        if mode not in ('packed_scalar','neon'):raise ValueError('Unknown packed kernel')
        path=Path(library) if library else Path(__file__).parent/'build_v2'/('fused_preprocess_v2.dll' if os.name=='nt' else 'libfused_preprocess_v2.so')
        super().__init__(color_lut,source_shape,library=path)
        packed=np.zeros((65536,4),np.uint8);packed[:,:3]=self.lut
        self.lut=packed.view('<u4').reshape(65536)
        self.lut.setflags(write=False)
        self.ys=np.ascontiguousarray(self.ys*self.width,dtype=np.int32)
        self.mode=mode
        for suffix in ('u8','fp32','i16'):
            function=getattr(self.library,'pl01_'+mode+'_'+suffix)
            function.argtypes=getattr(self.library,'pl01_fused_'+suffix).argtypes
            function.restype=ctypes.c_int
        self.library.pl01_has_neon.restype=ctypes.c_int
        self.native_neon=bool(self.library.pl01_has_neon())

    def _run(self,raw,name,output,values=None):
        source=np.asarray(raw)
        if source.ctypes.data&1:source=source.copy()
        candidate=name.replace('pl01_fused_','pl01_'+self.mode+'_')
        return super()._run(source,candidate,output,values)
