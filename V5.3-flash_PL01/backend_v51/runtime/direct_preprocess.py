"""Direct fixed-720P RGB565/BGR input. Runtime scales are external parameters."""
import ctypes
import os
from pathlib import Path
import numpy as np

def _axis(source,target,mirror=False,stride=1):
    # OpenCV casts the complete source coordinate to float BEFORE floor/subtract.
    coordinate=((np.arange(target,dtype=np.float64)+.5)*(source/target)-.5).astype(np.float32)
    index=np.floor(coordinate).astype(np.int32)
    fraction=(coordinate-index).astype(np.float32)
    weights=np.stack((np.rint((np.float32(1)-fraction)*np.float32(2048)),
                      np.rint(fraction*np.float32(2048))),axis=1).astype(np.int16)
    indices=np.stack((index,index+1),axis=1)
    if mirror:indices=source-1-indices
    return np.ascontiguousarray(indices*stride,dtype=np.int32),weights

class DirectPreprocessor:
    """Reusable output; consume before the next run. Source must be a stable raw copy."""
    def __init__(self,scale,bits=8,zero_point=0,mirror=True,library=None,output=None):
        if bits not in (8,16):raise ValueError('bits must be 8 or 16')
        if not np.isfinite(scale) or scale<=0:raise ValueError('positive finite scale required')
        if int(zero_point)!=zero_point:raise ValueError('integer zero point required')
        self.bits=bits
        self.xs,self.alpha=_axis(1280,384,mirror,2)
        self.ys,self.beta=_axis(720,216,stride=2560)
        self.xs_bgr,_=_axis(1280,384,mirror,3)
        self.ys_bgr,_=_axis(720,216,stride=3840)
        limit=2**(bits-1)-1
        # Reproduce inherited float32 /255, divide by float32 scale, rint ties-even,
        # symmetric signed saturation. Only this 256-entry table is floating point.
        values=np.arange(256,dtype=np.float32)/np.float32(255)
        self.qlut=np.clip(np.rint(values/np.float32(scale))+zero_point,-limit,limit).astype(np.int16)
        dtype=np.dtype(np.int8 if bits==8 else np.int16)
        self.output=np.empty((1,3,384,384),dtype) if output is None else output
        if self.output.dtype!=dtype or self.output.shape!=(1,3,384,384) or not self.output.flags.c_contiguous or not self.output.flags.writeable:
            raise ValueError('expected writable contiguous final integer NCHW output')
        native=Path(__file__).parent/'native'/('direct_preprocess.dll' if os.name=='nt' else 'libdirect_preprocess.so')
        self.library=ctypes.CDLL(str(library or native))
        self.fn=self.library.v32_rgb565_quantized
        self.fn.argtypes=[ctypes.c_void_p]*7+[ctypes.c_int]
        self.fn.restype=ctypes.c_int
        self.fn_bgr=self.library.v32_bgr_quantized
        self.fn_bgr.argtypes=[ctypes.c_void_p]*7+[ctypes.c_int]
        self.fn_bgr.restype=ctypes.c_int
        self.geometry=dict(width=1280,height=720,scale=.3,pad_x=0.,pad_y=84.,
                           resized_width=384,resized_height=216,inference_mirror=bool(mirror))
        self.fixed=[a.ctypes.data for a in (self.output,self.xs,self.ys,self.alpha,self.beta,self.qlut)]
        self.fixed_bgr=[a.ctypes.data for a in (self.output,self.xs_bgr,self.ys_bgr,self.alpha,self.beta,self.qlut)]

    def run(self,raw):
        raw=np.asarray(raw)
        if raw.dtype==np.dtype('<u2'):raw=raw.view(np.uint8)
        if raw.dtype!=np.uint8 or raw.size!=1843200 or not raw.flags.c_contiguous:
            raise ValueError('expected contiguous 1843200-byte little-endian RGB565 raw frame')
        status=self.fn(raw.ctypes.data,*self.fixed,self.bits)
        if status:raise RuntimeError('native preprocessor failed: %d'%status)
        return self.output,self.geometry

    def run_bgr(self,bgr):
        if (not isinstance(bgr,np.ndarray) or bgr.dtype!=np.uint8 or bgr.shape!=(720,1280,3)
                or not bgr.flags.c_contiguous):
            raise ValueError('expected contiguous uint8 BGR frame with shape (720,1280,3)')
        status=self.fn_bgr(bgr.ctypes.data,*self.fixed_bgr,self.bits)
        if status:raise RuntimeError('native BGR preprocessor failed: %d'%status)
        return self.output,self.geometry
