"""Pack camera-free PreparedUI coefficients for the independent PL plane."""
import cv2
import numpy as np

from .layers import PreparedUI


def pack_ui_plane(prepared, sequence, destination):
    """Write [P_B, P_G, P_R, I_B, I_G, I_R, seq_lo, seq_hi] per pixel.

    The caller owns the writable uint8 HWC8 destination and publishes it only
    after this function returns. Every call clears the old layer, including
    areas outside the new patches. No camera image is accepted or composed.
    Sequence is a nonzero 16-bit transport tag, independent of UI revision;
    buffer ownership, cache flush and presentation fencing belong to the driver.
    """
    update_ui_plane(prepared,sequence,destination)
    return destination


def _subtract(rect, cover):
    a,b,c,d=rect;e,f,g,h=cover
    x1,y1,x2,y2=max(a,e),max(b,f),min(c,g),min(d,h)
    if x1>=x2 or y1>=y2:return [rect]
    return [r for r in ((a,b,c,y1),(a,y2,c,d),(a,y1,x1,y2),(x2,y1,c,y2))
            if r[0]<r[2] and r[1]<r[3]]


def _uncovered(rect,covers):
    parts=[rect]
    for cover in covers:
        parts=[piece for part in parts for piece in _subtract(part,cover)]
        if not parts:break
    return parts


def _mapping(patch):
    # An immutable array's affine address in absolute screen coordinates.
    # Different rectangle partitions can still share exactly the same bytes.
    x,y,_,_=patch.rect
    return tuple((a.ctypes.data-y*a.strides[0]-x*a.strides[1],a.strides)
                 for a in (patch.premult,patch.inverse))


def update_ui_plane(prepared,sequence,destination,previous=None,*,previous_sequence=None):
    """Update one caller-owned inactive page from its own immutable snapshot.

    No pixel scan or camera access is needed to find changed coefficients.
    A driver may supply the last successfully packed tag for this exact page.
    Equal tags retain those bytes; unknown or changed tags rewrite every pixel.
    Counters are logical PS writes, not measured DDR traffic (cache-line
    writeback may be larger). Page ownership and presentation stay with driver.
    """
    if not isinstance(prepared, PreparedUI):
        raise TypeError('UI plane requires a camera-free PreparedUI packet')
    if type(sequence) is not int or not 1 <= sequence <= 65535:
        raise ValueError('UI plane sequence must be an integer in 1..65535')
    if previous_sequence is not None and (type(previous_sequence) is not int or not 1 <= previous_sequence <= 65535):
        raise ValueError('Previous UI plane sequence must be an integer in 1..65535')
    if (not isinstance(destination, np.ndarray) or destination.dtype != np.uint8
            or destination.shape != (prepared.height, prepared.width, 8)
            or not destination.flags.c_contiguous or not destination.flags.writeable):
        raise ValueError('UI plane destination must be writable contiguous uint8 HWC8 of packet dimensions')
    if previous is not None and (not isinstance(previous,PreparedUI) or
            (previous.width,previous.height)!=(prepared.width,prepared.height)):
        raise ValueError('Previous UI packet must have the same dimensions')

    if getattr(prepared,'native_geometry',False):
        from .native_ui import NativeGeometryUI,pack_geometry
        if not isinstance(prepared,NativeGeometryUI):
            raise TypeError('Native geometry requires the validated native packet type')
        retained=previous if isinstance(previous,NativeGeometryUI) and previous_sequence==sequence else None
        return pack_geometry(prepared,sequence,destination,retained)
    if getattr(prepared,'native_opaque',False):
        from .native_ui import NativeOpaqueUI, pack_opaque
        if not isinstance(prepared,NativeOpaqueUI):
            raise TypeError('Native UI requires the validated native packet type')
        retained = previous if isinstance(previous,NativeOpaqueUI) and previous_sequence==sequence else None
        return pack_opaque(prepared,sequence,destination,retained)
    if getattr(previous,'native_opaque',False):
        previous = None  # The generic patch list cannot describe native pixels.

    transparent = bytes((0, 0, 0, 255, 255, 255, sequence & 255, sequence >> 8))
    fill=np.frombuffer(transparent,np.uint64)[0]
    area=lambda r:(r[2]-r[0])*(r[3]-r[1])
    if previous is None:
        clears=[(0,0,prepared.width,prepared.height)]
        writes=[(p,p.rect) for p in prepared.patches]
    else:
        covers=[p.rect for p in prepared.patches]
        clears=[r for p in previous.patches for r in _uncovered(p.rect,covers)]
        same={}
        for p in previous.patches:same.setdefault(_mapping(p),[]).append(p.rect)
        writes=[(p,r) for p in prepared.patches
                for r in _uncovered(p.rect,same.get(_mapping(p),()))]
    words=destination.view(np.uint64)
    for x1,y1,x2,y2 in clears:
        words[y1:y2,x1:x2].fill(fill)
    for patch,(x1,y1,x2,y2) in writes:
        roi = destination[y1:y2, x1:x2]
        a,b,_,_=patch.rect;source=np.s_[y1-b:y2-b,x1-a:x2-a]
        cv2.mixChannels([patch.premult[source], patch.inverse[source]], [roi],
                        [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
    retag=previous is not None and previous_sequence!=sequence
    if retag:
        destination.view('<u2')[:,:,3].fill(sequence)
    cleared=sum(area(r) for r in clears);copied=sum(area(r) for _,r in writes)
    return dict(coefficient_pixels_written=cleared+copied,
                coefficient_bytes_written=(cleared+copied)*6,
                tag_pixels_written=prepared.width*prepared.height if previous is None or retag else cleared,
                coefficient_regions_written=len(clears)+len(writes),
                retained_page=previous is not None)
