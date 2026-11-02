"""Immutable camera-free UI packets and inexpensive live-video composition.

``tint`` uses cached colour/alpha coefficients only. Optional blur modes always
sample the current private camera frame before overlays: ``board_fast`` blurs
the declared narrow glass strip at 1/8 resolution; ``local_blur`` caps a panel
strip to 20 percent of the video. Neither mode stores a camera in a UI packet.
"""
from collections import OrderedDict, deque
from dataclasses import dataclass
import time

import cv2
import numpy as np


def _rect(value):
    values=tuple(int(v) for v in value)
    if len(values)!=4 or values[2]<=values[0] or values[3]<=values[1]:
        raise ValueError('An overlay rectangle needs positive dimensions')
    return values


def _readonly_pixels(value,shape):
    value=np.asarray(value)
    if value.dtype!=np.uint8 or value.shape!=shape:
        raise ValueError('Overlay coefficients must be uint8 HWC3 with rectangle dimensions')
    # A bytes owner, rather than just WRITEABLE=False on an owning ndarray,
    # prevents another thread from re-enabling writes after publication.
    return np.frombuffer(value.tobytes(order='C'),np.uint8).reshape(shape)


def _native_into(image,prepared):
    from .native_ui import NativeOpaqueUI,NativeGeometryUI
    if isinstance(prepared,NativeGeometryUI):
        x1,y1,x2,y2=prepared.viewport
        pixels=cv2.resize(prepared.bgr,(x2-x1,y2-y1),interpolation=cv2.INTER_NEAREST)
        mask=cv2.resize(prepared.mask,(x2-x1,y2-y1),interpolation=cv2.INTER_NEAREST)
        cv2.copyTo(pixels,mask,image[y1:y2,x1:x2])
        cv2.copyTo(prepared.controls.bgr,prepared.controls.mask,image)
        if prepared.pointer is not None:
            a,b,c,d=prepared.pointer_rect
            mask=cv2.inRange(prepared.pointer,(1,1,1),(255,255,255))
            cv2.copyTo(prepared.pointer,mask,image[b:d,a:c])
    elif isinstance(prepared,NativeOpaqueUI):
        cv2.copyTo(prepared.bgr,prepared.mask,image)
    else:
        raise TypeError('Invalid native UI packet')


def materialize_native_ui(prepared):
    """CPU preview/test adapter; the board packs native geometry directly."""
    image=np.zeros((prepared.height,prepared.width,3),np.uint8)
    _native_into(image,prepared)
    return image


@dataclass(frozen=True)
class OverlayPatch:
    rect: tuple
    premult: np.ndarray
    inverse: np.ndarray

    def __post_init__(self):
        rect=_rect(self.rect);shape=(rect[3]-rect[1],rect[2]-rect[0],3)
        object.__setattr__(self,'rect',rect)
        object.__setattr__(self,'premult',_readonly_pixels(self.premult,shape))
        object.__setattr__(self,'inverse',_readonly_pixels(self.inverse,shape))
        # Binary inverse is opaque foreground or unchanged live background.
        # Classify on publication; display never scans patch coefficients.
        inverse=self.inverse
        binary=np.all((inverse==0)|(inverse==255))
        same=np.array_equal(inverse[:,:,0],inverse[:,:,1]) and np.array_equal(inverse[:,:,0],inverse[:,:,2])
        mask=None
        if binary and same:
            foreground=inverse[:,:,0]==0
            if not np.any(self.premult[~foreground]):
                raw=np.where(foreground,255,0).astype(np.uint8)
                mask=np.frombuffer(raw.tobytes(),np.uint8).reshape(raw.shape)
        object.__setattr__(self,'copy_mask',mask)


@dataclass(frozen=True)
class PreparedUI:
    revision: int
    created_at: float
    width: int
    height: int
    patches: tuple
    glass_regions: tuple = ()

    def __post_init__(self):
        patches=tuple(self.patches);regions=tuple(_rect(r) for r in self.glass_regions)
        if self.width<=0 or self.height<=0 or self.revision<0:
            raise ValueError('Invalid UI packet dimensions or revision')
        for i,patch in enumerate(patches):
            if not isinstance(patch,OverlayPatch):raise TypeError('PreparedUI patches must be OverlayPatch instances')
            x1,y1,x2,y2=patch.rect
            if not (0<=x1<x2<=self.width and 0<=y1<y2<=self.height):raise ValueError('Overlay patch extends outside its video')
            for previous in patches[:i]:
                a=previous.rect
                if min(x2,a[2])>max(x1,a[0]) and min(y2,a[3])>max(y1,a[1]):
                    raise ValueError('PreparedUI patches must not overlap')
        object.__setattr__(self,'patches',patches);object.__setattr__(self,'glass_regions',regions)


def _stats(values):
    if not values:return dict(samples=0,mean=0.,p50=0.,p95=0.,maximum=0.)
    data=np.asarray(values,dtype=np.float64)
    return dict(samples=len(data),mean=float(data.mean()),p50=float(np.percentile(data,50)),
                p95=float(np.percentile(data,95)),maximum=float(data.max()))


class DisplayCompositor:
    """Own reusable byte workspaces; mutate only the display's private BGR frame."""
    def __init__(self,width,height,effects=None,glass_mode=None):
        self.width,self.height=int(width),int(height)
        self.effects=glass_mode or (effects.get('glass_mode',effects.get('mode','tint')) if isinstance(effects,dict) else effects) or 'tint'
        if self.effects not in ('tint','board_fast','local_blur'):raise ValueError('Unknown compositor effects mode')
        self._scratch=None  # Allocate only for legacy alpha patches.
        self._work=OrderedDict();self._blur=OrderedDict()
        self._timings={key:deque(maxlen=512) for key in ('effects_ms','blend_ms','total_ms')}
        self._frames=self._packet_changes=self._same_packet=self._blurred_pixels=0
        self._last_revision=None
        self._opaque_copies=self._alpha_blends=0

    def _workspace(self,shape):
        if self._scratch is None:
            self._scratch=np.empty((self.height,self.width,3),np.uint8)
        if shape not in self._work:
            # Views all share one display-owned allocation. A changed cursor
            # can change ROI dimensions every detector publication; no large
            # allocation is needed when those dimensions rotate through keys.
            self._work[shape]=self._scratch[:shape[0],:shape[1]]
            while len(self._work)>3:self._work.popitem(last=False)
        else:self._work.move_to_end(shape)
        return self._work[shape]

    def _effect_regions(self,prepared):
        if self.effects=='tint':return ()
        if self.effects=='board_fast':return prepared.glass_regions[:1]
        if not prepared.glass_regions:return ()
        # A packet bounding box can also enclose the floating close button and
        # transparent camera above the panel. Begin at the real panel strip.
        x1,y1,x2,_=prepared.glass_regions[0]
        cap=max(1,int(self.width*self.height*.20/max(1,x2-x1)))
        return ((x1,y1,x2,min(self.height,y1+cap)),)

    def _live_glass(self,image,prepared):
        for rect in self._effect_regions(prepared):
            x1,y1,x2,y2=rect
            x1,y1,x2,y2=max(0,x1),max(0,y1),min(self.width,x2),min(self.height,y2)
            if x2<=x1 or y2<=y1:continue
            roi=image[y1:y2,x1:x2];shape=roi.shape
            if shape not in self._blur:
                low_shape=max(1,shape[0]//8),max(1,shape[1]//8),3
                self._blur[shape]=(np.empty(low_shape,np.uint8),np.empty(low_shape,np.uint8),np.empty(shape,np.uint8))
                while len(self._blur)>2:self._blur.popitem(last=False)
            low,soft,up=self._blur[shape]
            cv2.resize(roi,(low.shape[1],low.shape[0]),dst=low,interpolation=cv2.INTER_AREA)
            cv2.GaussianBlur(low,(5,5),0,dst=soft)
            cv2.resize(soft,(shape[1],shape[0]),dst=up,interpolation=cv2.INTER_LINEAR)
            cv2.addWeighted(roi,.55,up,.45,0,dst=roi)
            self._blurred_pixels+=(x2-x1)*(y2-y1)

    def compose_into(self,private_bgr,prepared):
        started=time.perf_counter()
        if private_bgr.dtype!=np.uint8 or private_bgr.shape!=(self.height,self.width,3):
            raise ValueError('Display owns an independent uint8 BGR frame of configured dimensions')
        if prepared is None:
            self._frames+=1
            return private_bgr
        if (prepared.width,prepared.height)!=(self.width,self.height):raise ValueError('UI packet and video dimensions differ')
        self._live_glass(private_bgr,prepared);effect_done=time.perf_counter()
        if getattr(prepared,'native_opaque',False):
            _native_into(private_bgr,prepared)
            self._opaque_copies+=1
        for patch in prepared.patches:
            x1,y1,x2,y2=patch.rect;roi=private_bgr[y1:y2,x1:x2]
            if patch.copy_mask is not None:
                cv2.copyTo(patch.premult,patch.copy_mask,roi)
                self._opaque_copies+=1
            else:
                work=self._workspace(roi.shape)
                cv2.multiply(roi,patch.inverse,dst=work,scale=1/255.,dtype=cv2.CV_8U)
                cv2.add(work,patch.premult,dst=roi)
                self._alpha_blends+=1
        finished=time.perf_counter()
        if prepared.revision==self._last_revision:self._same_packet+=1
        else:self._packet_changes+=1
        self._last_revision=prepared.revision;self._frames+=1
        for key,value in (('effects_ms',effect_done-started),('blend_ms',finished-effect_done),('total_ms',finished-started)):
            self._timings[key].append(value*1000)
        return private_bgr

    def metrics(self):
        arrays=([self._scratch] if self._scratch is not None else [])+[a for buffers in self._blur.values() for a in buffers]
        return dict(frames_composed=self._frames,packet_changes=self._packet_changes,reused_packet_frames=self._same_packet,
            effects=self.effects,workspace_bytes=sum(a.nbytes for a in arrays),workspace_count=int(self._scratch is not None),roi_view_count=len(self._work),
            opaque_patch_copies=self._opaque_copies,alpha_patch_blends=self._alpha_blends,
            float_display_buffers=0,glass_pixels_processed=self._blurred_pixels,
            timings={key:_stats(value) for key,value in self._timings.items()},
            camera_source='current private video frame; no camera content in PreparedUI')
