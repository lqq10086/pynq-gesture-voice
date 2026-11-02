"""Immutable camera-free native opaque frames and caller-owned plane packing."""
from dataclasses import FrozenInstanceError
import math
import os

import cv2
import numpy as np

from spatial_build.native_renderer import NativeRaster,_unpublished_bytes
from .layers import PreparedUI


_NATIVE = None


def _native():
    global _NATIVE
    if _NATIVE is None:
        _NATIVE=NativeRaster('strict' if os.name=='nt' else 'neon')
    if not _NATIVE.features & 4:
        raise RuntimeError('Native motion/opaque feature is not compiled')
    return _NATIVE


def _pixels(value,shape,immutable=False):
    if (not isinstance(value,np.ndarray) or value.dtype!=np.uint8
            or value.shape!=shape or not value.flags.c_contiguous):
        raise ValueError('Native opaque pixels require contiguous uint8 with exact dimensions')
    if immutable:
        if not _immutable(value):
            raise ValueError('Published native opaque pixels must have an immutable bytes owner')


def _immutable(value):
    base=value
    while isinstance(base,np.ndarray): base=base.base
    return not value.flags.writeable and isinstance(base,bytes)


class NativeOpaqueUI(PreparedUI):
    """A fully generated opaque frame; no generic OverlayPatch is bypassed.

    RGB is zero wherever the binary mask is transparent. Native validation
    verifies these invariants and the exact active bounds before publication.
    The two bytes owners have no writable NumPy alias and are never reused.
    """
    native_opaque=True

    def __init__(self,revision,created_at,width,height,bgr,mask,bounds,geometry_pose=None,preset_id=None):
        super().__init__(int(revision),float(created_at),int(width),int(height),(),())
        if not math.isfinite(self.created_at): raise ValueError('Invalid native packet timestamp')
        _pixels(bgr,(self.height,self.width,3),True)
        _pixels(mask,(self.height,self.width),True)
        bounds=tuple(int(v) for v in bounds)
        if len(bounds)!=4 or not (0<=bounds[0]<=bounds[2]<=self.width and 0<=bounds[1]<=bounds[3]<=self.height):
            raise ValueError('Invalid native opaque bounds')
        raw_bounds=np.asarray(bounds,np.int32)
        code=_native().library.validate_opaque(bgr.ctypes.data,mask.ctypes.data,
            self.width,self.height,raw_bounds.ctypes.data)
        if code: raise ValueError('Invalid native opaque coefficients or bounds: %d' % code)
        if geometry_pose is not None:
            geometry_pose=tuple(float(v) for v in geometry_pose)
            if len(geometry_pose)!=3 or not all(math.isfinite(v) for v in geometry_pose):
                raise ValueError('Invalid native geometry pose')
        object.__setattr__(self,'bgr',bgr);object.__setattr__(self,'mask',mask)
        object.__setattr__(self,'bounds',bounds);object.__setattr__(self,'geometry_pose',geometry_pose)
        object.__setattr__(self,'preset_id',None if preset_id is None else str(preset_id))
        # Only compose_opaque attaches immutable source provenance. A manually
        # constructed valid packet has no proof that pixels outside a viewport
        # stayed unchanged, so it always receives a full-frame comparison.
        object.__setattr__(self,'_template_bgr',None);object.__setattr__(self,'_template_mask',None)
        object.__setattr__(self,'_viewport',None);object.__setattr__(self,'_pointer_rect',None)

    def __setattr__(self,name,value): raise FrozenInstanceError('NativeOpaqueUI is immutable')
    def __delattr__(self,name): raise FrozenInstanceError('NativeOpaqueUI is immutable')

    def with_revision(self,revision,now):
        result=NativeOpaqueUI(revision,now,self.width,self.height,self.bgr,self.mask,self.bounds,self.geometry_pose,self.preset_id)
        for name in ('_template_bgr','_template_mask','_viewport','_pointer_rect'):
            object.__setattr__(result,name,getattr(self,name))
        return result


def compose_opaque(geometry,coverage,viewport,controls,revision,now,
                   pointer=None,pointer_rect=None,geometry_pose=None,preset_id=None,
                   geometry_scale=1,controls_mask=None):
    """Native full initialization from geometry, cached controls and one pointer.

    Inputs are private UI data, never camera pixels. Only successful native
    output receives NumPy views or a published packet.
    """
    if not isinstance(controls,np.ndarray) or controls.ndim!=3:
        raise ValueError('Native controls require a BGR canvas')
    height,width=controls.shape[:2]
    _pixels(controls,(height,width,3))
    vx,vy,x2,y2=map(int,viewport);gw,gh=x2-vx,y2-vy
    if not (0<=vx<x2<=width and 0<=vy<y2<=height): raise ValueError('Invalid geometry viewport')
    if type(geometry_scale) is not int or geometry_scale not in (1,2):
        raise ValueError('Native geometry scale must be one or two')
    low_width=(gw+geometry_scale-1)//geometry_scale;low_height=(gh+geometry_scale-1)//geometry_scale
    _pixels(geometry,(low_height,low_width,3));_pixels(coverage,(low_height,low_width))
    if controls_mask is None:
        controls_mask=cv2.inRange(controls,(1,1,1),(255,255,255))
        controls=cv2.bitwise_and(controls,controls,mask=controls_mask)
    else: _pixels(controls_mask,(height,width))
    if pointer is None:
        if pointer_rect is not None: raise ValueError('Pointer bounds require pointer pixels')
        px=py=pw=ph=0;pointer_address=None
    else:
        if pointer_rect is None: raise ValueError('Pointer pixels require pointer bounds')
        px,py,p2,q2=map(int,pointer_rect);pw,ph=p2-px,q2-py
        if not (0<=px<p2<=width and 0<=py<q2<=height): raise ValueError('Invalid pointer bounds')
        _pixels(pointer,(ph,pw,3));pointer_address=pointer.ctypes.data
    native=_native()
    if not native.features & 8: raise RuntimeError('Native scaled opaque feature is not compiled')
    bgr_owner,bgr_address=_unpublished_bytes(height*width*3)
    mask_owner,mask_address=_unpublished_bytes(height*width)
    bounds=np.empty(4,np.int32)
    code=native.library.compose_opaque_scaled(geometry.ctypes.data,coverage.ctypes.data,vx,vy,gw,gh,geometry_scale,
        controls.ctypes.data,controls_mask.ctypes.data,pointer_address,px,py,pw,ph,width,height,
        bgr_address,mask_address,bounds.ctypes.data)
    if code: raise RuntimeError('Native opaque composition failed: %d' % code)
    bgr=np.frombuffer(bgr_owner,np.uint8).reshape(height,width,3)
    mask=np.frombuffer(mask_owner,np.uint8).reshape(height,width)
    result=NativeOpaqueUI(revision,now,width,height,bgr,mask,bounds,geometry_pose,preset_id)
    if _immutable(controls) and _immutable(controls_mask):
        object.__setattr__(result,'_template_bgr',controls);object.__setattr__(result,'_template_mask',controls_mask)
        object.__setattr__(result,'_viewport',(vx,vy,x2,y2))
        object.__setattr__(result,'_pointer_rect',None if pointer is None else (px,py,px+pw,py+ph))
    return result


def pack_opaque(prepared,tag,destination,previous=None):
    """Write only caller-owned inactive HWC8 storage; no flush or publication.

    A previous packet is valid only for this exact page with its unchanged tag.
    Without that history every pixel, including transparent pixels, is written.
    """
    if not isinstance(prepared,NativeOpaqueUI): raise TypeError('Expected a native opaque packet')
    if type(tag) is not int or not 1<=tag<=65535: raise ValueError('Invalid native plane tag')
    _pixels(destination,(prepared.height,prepared.width,8))
    if not destination.flags.writeable: raise ValueError('Native plane destination must be private writable storage')
    previous_bgr=previous_mask=None
    if previous is not None:
        if not isinstance(previous,NativeOpaqueUI) or (previous.width,previous.height)!=(prepared.width,prepared.height):
            raise ValueError('Native plane history requires the same packet dimensions')
        previous_bgr=previous.bgr.ctypes.data;previous_mask=previous.mask.ctypes.data
    dirty=np.empty(2,np.int32);stats=np.empty(4,np.uint64);native=_native()
    arguments=(prepared.bgr.ctypes.data,prepared.mask.ctypes.data,destination.ctypes.data,
        prepared.width,prepared.height,tag,previous_bgr,previous_mask)
    regions=[]
    if (previous is not None and native.features & 8 and prepared._template_bgr is not None
            and previous._template_bgr is prepared._template_bgr and previous._template_mask is prepared._template_mask
            and previous._viewport==prepared._viewport):
        regions=[prepared._viewport]
        regions.extend(rect for rect in (previous._pointer_rect,prepared._pointer_rect) if rect is not None)
        raw_regions=np.asarray(regions,np.int32)
        code=native.library.pack_opaque_regions(*arguments,raw_regions.ctypes.data,len(regions),dirty.ctypes.data,stats.ctypes.data)
    else:
        code=native.library.pack_opaque(*arguments,dirty.ctypes.data,stats.ctypes.data)
    if code: raise RuntimeError('Native plane packing failed: %d' % code)
    return dict(dirty_first_row=int(dirty[0]),dirty_last_row=int(dirty[1]),
        coefficient_pixels_written=int(stats[0]),coefficient_bytes_written=int(stats[1]),
        tag_pixels_written=int(stats[2]),coefficient_regions_written=int(stats[0]>0),
        retained_page=previous is not None,native_pack_simd_pixels=int(stats[3]),
        native_pack_scope='geometry_and_pointer' if regions else 'full_frame')


class NativeGeometryUI(PreparedUI):
    """Sparse immutable motion state; no full-frame pixels are constructed.

    Controls retain their already validated full-frame bytes. Each pose only
    validates its low-resolution geometry and optional tiny pointer patch.
    """
    native_opaque=True
    native_geometry=True

    def __init__(self,revision,created_at,bgr,mask,viewport,controls,pixel_scale=2,
                 pointer=None,pointer_rect=None,geometry_pose=None,preset_id=None):
        if not isinstance(controls,NativeOpaqueUI): raise TypeError('Geometry controls must be a validated NativeOpaqueUI template')
        super().__init__(int(revision),float(created_at),controls.width,controls.height,(),())
        if not math.isfinite(self.created_at): raise ValueError('Invalid geometry packet timestamp')
        if type(pixel_scale) is not int or pixel_scale not in (1,2): raise ValueError('Geometry pixel scale must be one or two')
        viewport=tuple(int(v) for v in viewport)
        if len(viewport)!=4 or not (0<=viewport[0]<viewport[2]<=self.width and 0<=viewport[1]<viewport[3]<=self.height):
            raise ValueError('Invalid native geometry viewport')
        vx,vy,x2,y2=viewport
        gw,gh=(x2-vx+pixel_scale-1)//pixel_scale,(y2-vy+pixel_scale-1)//pixel_scale
        _pixels(bgr,(gh,gw,3),True);_pixels(mask,(gh,gw),True)
        pointer_address=None;pw=ph=0
        if pointer is None:
            if pointer_rect is not None: raise ValueError('Pointer bounds require pointer pixels')
        else:
            if pointer_rect is None: raise ValueError('Pointer pixels require bounds')
            pointer_rect=tuple(int(v) for v in pointer_rect)
            if len(pointer_rect)!=4 or not (0<=pointer_rect[0]<pointer_rect[2]<=self.width and 0<=pointer_rect[1]<pointer_rect[3]<=self.height):
                raise ValueError('Invalid geometry pointer bounds')
            px,py,p2,q2=pointer_rect;pw,ph=p2-px,q2-py
            _pixels(pointer,(ph,pw,3),True);pointer_address=pointer.ctypes.data
        native=_native()
        if not native.features & 16: raise RuntimeError('Sparse native geometry feature is not compiled')
        code=native.library.validate_geometry(bgr.ctypes.data,mask.ctypes.data,gw,gh,pointer_address,pw,ph)
        if code: raise ValueError('Invalid native geometry coefficients: %d' % code)
        if geometry_pose is not None:
            geometry_pose=tuple(float(v) for v in geometry_pose)
            if len(geometry_pose)!=3 or not all(math.isfinite(v) for v in geometry_pose): raise ValueError('Invalid native geometry pose')
        for name,value in (('bgr',bgr),('mask',mask),('viewport',viewport),('controls',controls),
                ('pixel_scale',pixel_scale),('pointer',pointer),('pointer_rect',pointer_rect),
                ('geometry_pose',geometry_pose),('preset_id',None if preset_id is None else str(preset_id))):
            object.__setattr__(self,name,value)

    def __setattr__(self,name,value): raise FrozenInstanceError('NativeGeometryUI is immutable')
    def __delattr__(self,name): raise FrozenInstanceError('NativeGeometryUI is immutable')

    def with_revision(self,revision,now):
        return NativeGeometryUI(revision,now,self.bgr,self.mask,self.viewport,self.controls,self.pixel_scale,
            self.pointer,self.pointer_rect,self.geometry_pose,self.preset_id)


def _freeze_pixels(value):
    return value if _immutable(value) else np.frombuffer(value.tobytes(order='C'),value.dtype).reshape(value.shape)


def prepare_controls(bgr,mask):
    """Freeze and fully validate one static template per controls cache key."""
    if not isinstance(bgr,np.ndarray) or bgr.ndim!=3: raise ValueError('Controls require BGR pixels')
    height,width=bgr.shape[:2]
    _pixels(bgr,(height,width,3));_pixels(mask,(height,width))
    bgr,mask=_freeze_pixels(bgr),_freeze_pixels(mask)
    x,y,w,h=cv2.boundingRect(mask)
    bounds=(x,y,x+w,y+h) if w and h else (0,0,0,0)
    return NativeOpaqueUI(0,0.,width,height,bgr,mask,bounds)


def compose_geometry(geometry,coverage,viewport,controls_template,revision,now,
                     pointer=None,pointer_rect=None,geometry_pose=None,preset_id=None,pixel_scale=2):
    """Publish only immutable low geometry, a validated template and tiny UI."""
    if not isinstance(geometry,np.ndarray) or not isinstance(coverage,np.ndarray): raise ValueError('Geometry requires NumPy pixels')
    geometry,coverage=_freeze_pixels(geometry),_freeze_pixels(coverage)
    if pointer is not None:
        if not isinstance(pointer,np.ndarray) or pointer.dtype!=np.uint8 or pointer.ndim!=3 or pointer.shape[2]!=3:
            raise ValueError('Pointer requires uint8 BGR pixels')
        # The original opaque UI discards a pixel unless all channels are >0.
        # Normalize the tiny patch once, then retain bytes with no writable alias.
        active=cv2.inRange(pointer,(1,1,1),(255,255,255))
        pointer=_freeze_pixels(cv2.bitwise_and(pointer,pointer,mask=active))
    return NativeGeometryUI(revision,now,geometry,coverage,viewport,controls_template,pixel_scale,
        pointer,pointer_rect,geometry_pose,preset_id)


def materialize_geometry(prepared):
    """Explicit full-frame reference for PC display/tests; never used by pack."""
    if not isinstance(prepared,NativeGeometryUI): raise TypeError('Expected a sparse geometry packet')
    return compose_opaque(prepared.bgr,prepared.mask,prepared.viewport,prepared.controls.bgr,
        prepared.revision,prepared.created_at,prepared.pointer,prepared.pointer_rect,
        prepared.geometry_pose,prepared.preset_id,geometry_scale=prepared.pixel_scale,controls_mask=prepared.controls.mask)


def pack_geometry(prepared,tag,destination,previous=None):
    """Update an inactive plane directly from low-resolution immutable state."""
    if not isinstance(prepared,NativeGeometryUI): raise TypeError('Expected a sparse geometry packet')
    if type(tag) is not int or not 1<=tag<=65535: raise ValueError('Invalid geometry plane tag')
    _pixels(destination,(prepared.height,prepared.width,8))
    if not destination.flags.writeable: raise ValueError('Geometry plane destination must be private writable storage')
    reusable=(isinstance(previous,NativeGeometryUI) and previous.controls is prepared.controls
        and previous.viewport==prepared.viewport and previous.pixel_scale==prepared.pixel_scale)
    px=py=pw=ph=opx=opy=opw=oph=0
    pointer=old_rgb=old_mask=old_pointer=None
    if prepared.pointer is not None:
        px,py,p2,q2=prepared.pointer_rect;pw,ph=p2-px,q2-py;pointer=prepared.pointer.ctypes.data
    if reusable:
        old_rgb=previous.bgr.ctypes.data;old_mask=previous.mask.ctypes.data
        if previous.pointer is not None:
            opx,opy,p2,q2=previous.pointer_rect;opw,oph=p2-opx,q2-opy;old_pointer=previous.pointer.ctypes.data
    vx,vy,x2,y2=prepared.viewport
    layout=np.asarray((prepared.width,prepared.height,vx,vy,x2-vx,y2-vy,prepared.pixel_scale,tag,
        px,py,pw,ph,opx,opy,opw,oph,int(reusable)),np.int32)
    dirty=np.empty(2,np.int32);stats=np.empty(6,np.uint64)
    code=_native().library.pack_geometry(prepared.bgr.ctypes.data,prepared.mask.ctypes.data,
        prepared.controls.bgr.ctypes.data,prepared.controls.mask.ctypes.data,pointer,
        old_rgb,old_mask,old_pointer,layout.ctypes.data,destination.ctypes.data,dirty.ctypes.data,stats.ctypes.data)
    if code: raise RuntimeError('Sparse native geometry packing failed: %d' % code)
    return dict(dirty_first_row=int(dirty[0]),dirty_last_row=int(dirty[1]),
        coefficient_pixels_written=int(stats[0]),coefficient_bytes_written=int(stats[1]),
        tag_pixels_written=int(stats[2]),coefficient_regions_written=int(stats[0]>0),retained_page=bool(reusable),
        native_pack_simd_pixels=int(stats[3]),native_geometry_low_pixels_tested=int(stats[4]),
        native_geometry_pixel_write_events=int(stats[5]),native_geometry_full_init=not reusable,
        native_pack_scope='lowres_geometry_delta')
