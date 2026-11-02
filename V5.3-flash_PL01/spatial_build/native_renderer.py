"""Real native raster binding; missing/incorrect native code is a hard error."""
import ctypes
import math
import os
from pathlib import Path

import numpy as np


LIBRARY = Path(__file__).with_name('native')/('renderer_core.dll' if os.name=='nt' else 'librenderer_core.so')


def _unpublished_bytes(size):
    """Private CPython construction storage; never expose it before C succeeds.

    Only the returned local owner keeps this allocation alive. The address is
    borrowed for one construction call, never retained or used after publishing
    a NumPy view. No hash, view, or external reference exists during filling.
    """
    allocate = ctypes.pythonapi.PyBytes_FromStringAndSize
    allocate.argtypes = [ctypes.c_void_p,ctypes.c_ssize_t]
    allocate.restype = ctypes.py_object
    address = ctypes.pythonapi.PyBytes_AsString
    address.argtypes = [ctypes.py_object]
    address.restype = ctypes.c_void_p
    owner = allocate(None,size)
    return owner,address(owner)


class NativeRaster:
    def __init__(self, mode='neon', library=LIBRARY):
        if mode not in ('strict', 'neon'):
            raise ValueError('Expected strict scalar or guarded ARM NEON raster')
        self.library = ctypes.CDLL(str(library))
        self.library.renderer_abi.restype = ctypes.c_uint32
        self.library.renderer_features.restype = ctypes.c_uint32
        if self.library.renderer_abi() != 0x52334402:
            raise ValueError('Native renderer ABI mismatch')
        features = int(self.library.renderer_features())
        if not features & 1 or (mode=='neon' and not features & 2):
            raise ValueError('Requested native renderer kernel is not compiled into this library')
        self.function = self.library.raster_faces
        self.function.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        self.function.restype = ctypes.c_int
        self.mode, self.features, self.calls = mode, features, 0
        # NumPy 1.x may value-promote these scalar coefficients to float32;
        # NumPy 2.x preserves their float64 dtype. Match the active reference.
        probe=np.ones(1,np.float32)
        self.reference32=(np.float64(.7)*probe).dtype==np.float32
        self.stats = np.zeros(3, np.uint64)
        if features & 4:
            specs = {
                'project_motion_faces': [ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,
                    ctypes.c_double,*([ctypes.c_int]*5),*([ctypes.c_void_p]*4)],
                'raster_motion': [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,
                    *([ctypes.c_void_p]*3),ctypes.c_int,ctypes.c_int,
                    *([ctypes.c_void_p]*5),*([ctypes.c_int]*3),ctypes.c_void_p,ctypes.c_void_p],
                'compose_opaque': [ctypes.c_void_p,ctypes.c_void_p,*([ctypes.c_int]*4),
                    ctypes.c_void_p,ctypes.c_void_p,*([ctypes.c_int]*6),*([ctypes.c_void_p]*3)],
                'validate_opaque': [ctypes.c_void_p,ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_void_p],
                'pack_opaque': [*([ctypes.c_void_p]*3),*([ctypes.c_int]*3),*([ctypes.c_void_p]*4)],
            }
            for name,arguments in specs.items():
                function=getattr(self.library,name)
                function.argtypes=arguments;function.restype=ctypes.c_int
        if features & 8:
            self.library.compose_opaque_scaled.argtypes=[ctypes.c_void_p,ctypes.c_void_p,
                *([ctypes.c_int]*5),*([ctypes.c_void_p]*3),*([ctypes.c_int]*6),*([ctypes.c_void_p]*3)]
            self.library.compose_opaque_scaled.restype=ctypes.c_int
            self.library.pack_opaque_regions.argtypes=[*([ctypes.c_void_p]*3),*([ctypes.c_int]*3),
                *([ctypes.c_void_p]*3),ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p]
            self.library.pack_opaque_regions.restype=ctypes.c_int
        if features & 16:
            self.library.validate_geometry.argtypes=[ctypes.c_void_p,ctypes.c_void_p,
                ctypes.c_int,ctypes.c_int,ctypes.c_void_p,ctypes.c_int,ctypes.c_int]
            self.library.validate_geometry.restype=ctypes.c_int
            self.library.pack_geometry.argtypes=[ctypes.c_void_p]*12
            self.library.pack_geometry.restype=ctypes.c_int

    def project_motion(self, mesh, basis, unit, width, height, vx, vy, selected):
        if not self.features & 4: raise RuntimeError('Native motion/opaque feature is not compiled')
        mesh=np.ascontiguousarray(mesh,np.int32).reshape(-1,6)
        basis=np.ascontiguousarray(basis,np.float64).reshape(12)
        faces=np.empty((144,11),np.int32);planes=np.empty((144,4),np.float64)
        polygons=np.empty((144,4,2),np.float64);indices=np.empty(144,np.int32)
        count=self.library.project_motion_faces(mesh.ctypes.data,len(mesh),basis.ctypes.data,
            unit,width,height,vx,vy,selected,faces.ctypes.data,planes.ctypes.data,
            polygons.ctypes.data,indices.ctypes.data)
        if count<0: raise RuntimeError('Native motion projection failed: %d' % count)
        return faces[:count],planes[:count],polygons[:count],indices[:count]

    def raster_immutable(self, faces, planes, basis, unit, width, height, selected):
        if not self.features & 4: raise RuntimeError('Native motion/opaque feature is not compiled')
        faces=np.ascontiguousarray(faces,np.int32).reshape(-1,11)
        planes=np.ascontiguousarray(planes,np.float64).reshape(-1,4)
        basis=np.ascontiguousarray(basis,np.float64).reshape(12)
        if len(faces)!=len(planes) or len(faces)>144 or not np.isfinite(planes).all() or not np.isfinite(basis).all():
            raise ValueError('Invalid native motion face coefficients')
        if width<=0 or height<=0 or width*height>0x7fffffff//8:
            raise ValueError('Invalid native motion dimensions')
        specifications=(((height,width,3),np.uint8),((height,width),np.uint8),
            ((height,width),np.int16),((height,width),np.int16),((height,width),np.float32))
        storage=[_unpublished_bytes(math.prod(shape)*np.dtype(dtype).itemsize) for shape,dtype in specifications]
        rx=(np.arange(width,dtype=np.float32)+.5-width*.5)/unit
        uy=(height*.5-np.arange(height,dtype=np.float32)-.5)/unit
        counts=np.empty(144,np.uint32)
        code=self.library.raster_motion(faces.ctypes.data,planes.ctypes.data,len(faces),
            rx.ctypes.data,uy.ctypes.data,basis.ctypes.data,width,height,
            *(address for _,address in storage),int(self.mode=='neon'),int(self.reference32),selected,
            counts.ctypes.data,self.stats.ctypes.data)
        if code: raise RuntimeError('Native immutable motion raster failed: %d' % code)
        self.calls+=1
        # Publication happens only after the complete successful native write.
        result=tuple(np.frombuffer(owner,dtype).reshape(shape)
                     for (owner,_),(shape,dtype) in zip(storage,specifications))
        return (*result,counts[:len(faces)])

    def raster(self, faces, planes, basis, unit, bgr, alpha, ids, owners, depth):
        height, width = depth.shape
        expected = ((bgr, np.uint8, (height,width,3)), (alpha, np.uint8, (height,width)),
                    (ids, np.int16, (height,width)), (owners, np.int16, (height,width)),
                    (depth, np.float32, (height,width)))
        for value, dtype, shape in expected:
            if value.dtype != dtype or value.shape != shape or not value.flags.c_contiguous or not value.flags.writeable:
                raise ValueError('Native raster requires private contiguous writable buffers')
        faces = np.ascontiguousarray(faces, np.int32).reshape(-1,11)
        planes = np.ascontiguousarray(planes, np.float64).reshape(-1,4)
        basis = np.ascontiguousarray(basis, np.float64).reshape(12)
        if len(faces) != len(planes) or len(faces)>144 or not np.isfinite(planes).all() or not np.isfinite(basis).all():
            raise ValueError('Invalid native face coefficients')
        # Preserve the reference's float32 pixel-centre coordinate arithmetic.
        rx = (np.arange(width,dtype=np.float32)+.5-width*.5)/unit
        uy = (height*.5-np.arange(height,dtype=np.float32)-.5)/unit
        code = self.function(faces.ctypes.data, planes.ctypes.data, len(faces),
            rx.ctypes.data, uy.ctypes.data, basis.ctypes.data, width, height,
            bgr.ctypes.data, alpha.ctypes.data, ids.ctypes.data, owners.ctypes.data,
            depth.ctypes.data, int(self.mode=='neon'), int(self.reference32), self.stats.ctypes.data)
        if code:
            raise RuntimeError('Native raster failed: %d' % code)
        self.calls += 1

    def metrics(self):
        return dict(backend='ARM_NEON_FP32_GUARDED' if self.mode=='neon' else 'NATIVE_SCALAR_REFERENCE',
                    reference_math='float32' if self.reference32 else 'float64',
                    native_calls=self.calls, feature_bits=self.features,
                    last_pixel_tests=int(self.stats[0]), last_simd_lanes=int(self.stats[1]),
                    last_boundary_refinements=int(self.stats[2]))
