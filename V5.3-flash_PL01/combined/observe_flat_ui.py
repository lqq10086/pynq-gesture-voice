"""Flat opaque UI prepared once per state change; no live-camera dependency."""
import math
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


def ui_regions(app):
    """Conservative draw bounds; debug annotations may occupy the whole frame."""
    if app.debug:
        return None
    regions = [app.layout['close']]
    if app.stage == 'levels':
        regions.extend(app.level_rects.values())
    else:
        layout = app.spatial.layout
        regions.extend(layout[key] for key in ('observe', 'view_reset', 'replay'))
        x1,y1,x2,y2 = layout['palm_hint']
        cx,cy = (x1+x2)*.5,(y1+y2)*.5
        reach = (y2-y1)*.28*2.35
        regions.append((math.floor(cx-reach)-3, math.floor(cy-reach)-3,
                        math.ceil(cx+reach)+4, math.ceil(cy+reach)+4))
    if app.controls.pointer is not None:
        x,y = map(int, app.controls.pointer)
        radius = max(3,int(app.height*.006))+2
        regions.append((x-radius,y-radius,x+radius+1,y+radius+1))
    return tuple(regions)


def opaque_packet(image, revision, now, regions=None):
    """Partition one black-backed UI draw into small non-overlapping byte tiles.

    Black is reserved for empty canvas, not for solid UI fills. No camera is
    read here; an inactive pixel is never composited. Copy masks are immutable
    and cached in each patch, so display performs only cv2.copyTo on dirty UI.
    """
    height,width=image.shape[:2]
    active=cv2.inRange(image,(1,1,1),(255,255,255)) if regions is None else None
    patches=[]
    tile=256
    for y in range(0,height,tile):
        for x in range(0,width,tile):
            mx,my=x,y
            if active is not None:
                mask=active[y:min(y+tile,height),x:min(x+tile,width)]
            else:
                intersections=[(max(x,a),max(y,b),min(x+tile,width,c),min(y+tile,height,d))
                               for a,b,c,d in regions]
                intersections=[rect for rect in intersections if rect[0]<rect[2] and rect[1]<rect[3]]
                if not intersections:continue
                mx,my=min(r[0] for r in intersections),min(r[1] for r in intersections)
                right,bottom=max(r[2] for r in intersections),max(r[3] for r in intersections)
                mask=np.zeros((bottom-my,right-mx),np.uint8)
                for a,b,c,d in intersections:
                    cv2.inRange(image[b:d,a:c],(1,1,1),(255,255,255),
                                dst=mask[b-my:d-my,a-mx:c-mx])
            bx,by,bw,bh=cv2.boundingRect(mask)
            if not bw or not bh:continue
            x1,y1=mx+bx,my+by
            rect=(x1,y1,x1+bw,y1+bh)
            foreground=mask[by:by+bh,bx:bx+bw]
            inverse=cv2.cvtColor(cv2.bitwise_not(foreground),cv2.COLOR_GRAY2BGR)
            pixels=cv2.bitwise_and(image[y1:y1+bh,x1:x1+bw],
                                  image[y1:y1+bh,x1:x1+bw],mask=foreground)
            patches.append(OverlayPatch(rect,pixels,inverse))
    return PreparedUI(int(revision),float(now),width,height,tuple(patches),())
