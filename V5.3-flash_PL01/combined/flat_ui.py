"""Opaque foreground with an optional local alpha layer; no camera dependency."""
import cv2
import numpy as np
from spatial_build.ui import Ui
from town_creator.layers import OverlayPatch, PreparedUI


class FlatUi(Ui):
    def panel(self, image, rect, color=(76, 76, 76), alpha=1.0, radius=None,
              shadow=False, border=False):
        # Existing button callers still supply alpha; intentionally opaque now.
        x1,y1,x2,y2=map(int,rect)
        ix1,iy1,ix2,iy2=max(0,x1),max(0,y1),min(image.shape[1],x2),min(image.shape[0],y2)
        if ix2<=ix1 or iy2<=iy1:return
        width,height=x2-x1,y2-y1
        radius=min(16,int(min(width,height)*.16)) if radius is None else radius
        mask=self._round(width,height,radius)[iy1-y1:iy2-y1,ix1-x1:ix2-x1]
        gray=int(round(sum(color)/3))
        key=((iy2-iy1,ix2-ix1,3),(gray,gray,gray))
        if key not in self._solid:
            self._remember(self._solid,key,np.full(key[0],gray,np.uint8),48)
        cv2.copyTo(self._solid[key],mask,image[iy1:iy2,ix1:ix2])


def opaque_packet(image, revision, now, background=None):
    """Partition one black-backed UI draw into small non-overlapping byte tiles.

    Black is reserved for empty foreground canvas, not for solid UI fills.
    The optional background is one bounded (rect, premult, inverse) layer;
    foreground pixels remain opaque. Coefficients go directly to the PL plane.
    """
    height,width=image.shape[:2]
    active=cv2.inRange(image,(1,1,1),(255,255,255))
    if background is not None:
        (a,b,c,d),base,inverse_base=background
    patches=[]
    tile=256
    for y in range(0,height,tile):
        for x in range(0,width,tile):
            mask=active[y:min(y+tile,height),x:min(x+tile,width)]
            if background is not None:
                left,top,right,bottom=max(x,a),max(y,b),min(x+mask.shape[1],c),min(y+mask.shape[0],d)
                if right>left and bottom>top:
                    mask=mask.copy()
                    target=mask[top-y:bottom-y,left-x:right-x]
                    source=inverse_base[top-b:bottom-b,left-a:right-a]
                    cv2.bitwise_or(target,cv2.inRange(source,(0,0,0),(254,254,254)),dst=target)
            bx,by,bw,bh=cv2.boundingRect(mask)
            if not bw or not bh:continue
            x1,y1=x+bx,y+by
            rect=(x1,y1,x1+bw,y1+bh)
            if background is None:
                inverse=np.repeat((255-mask[by:by+bh,bx:bx+bw])[:,:,None],3,axis=2)
                pixels=image[y1:y1+bh,x1:x1+bw].copy()
                pixels[mask[by:by+bh,bx:bx+bw]==0]=0
                patches.append(OverlayPatch(rect,pixels,inverse))
                continue
            pixels=np.zeros((bh,bw,3),np.uint8)
            inverse=np.full_like(pixels,255)
            if background is not None:
                left,top,right,bottom=max(x1,a),max(y1,b),min(x1+bw,c),min(y1+bh,d)
                if right>left and bottom>top:
                    target=np.s_[top-y1:bottom-y1,left-x1:right-x1]
                    source=np.s_[top-b:bottom-b,left-a:right-a]
                    pixels[target]=base[source]
                    inverse[target]=inverse_base[source]
            foreground=active[y1:y1+bh,x1:x1+bw]
            cv2.copyTo(image[y1:y1+bh,x1:x1+bw],foreground,pixels)
            inverse[foreground!=0]=0
            patches.append(OverlayPatch(rect,pixels,inverse))
    return PreparedUI(int(revision),float(now),width,height,tuple(patches),())
