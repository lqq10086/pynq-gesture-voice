"""Low-cost camera-overlay UI: cached rounded masks and rasterized font masks.

There is no frame blur and no full-frame Pillow conversion. Pillow is used only
when a new (text, size) enters the small cache; compositing stays inside text and
panel rectangles. Colours are BGR, and text points refer to the glyph top edge.
"""
from collections import OrderedDict
from pathlib import Path
import math

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont


class Ui:
    def __init__(self, width, height, font_path=None):
        self.width, self.height = int(width), int(height)
        self.font_path = self._font_path(font_path)
        self._fonts = {}
        self._texts = OrderedDict()
        self._masks = OrderedDict()
        self._solid = OrderedDict()
        self._work = {}

    @staticmethod
    def _font_path(explicit):
        root = Path(__file__).resolve().parents[1]
        candidates = ([Path(explicit)] if explicit else []) + [
            root/'assets/ui_font.ttf',
            Path('C:/Windows/Fonts/msyh.ttc'),
            Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
            Path('/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc'),
            Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        raise RuntimeError('No UI font found. Keep the packaged assets/ui_font.ttf.')

    @staticmethod
    def _remember(cache, key, value, capacity):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > capacity:
            cache.popitem(last=False)
        return value

    def _round(self, width, height, radius, outline=False):
        radius = max(0, min(int(radius), width//2, height//2))
        key = width,height,radius,bool(outline)
        if key in self._masks:
            self._masks.move_to_end(key)
            return self._masks[key]
        mask = np.zeros((height,width),np.uint8)
        if width < 2 or height < 2:
            return mask
        cv2.rectangle(mask,(radius,0),(width-radius-1,height-1),255,-1)
        cv2.rectangle(mask,(0,radius),(width-1,height-radius-1),255,-1)
        for x in (radius,width-radius-1):
            for y in (radius,height-radius-1):
                cv2.circle(mask,(x,y),radius,255,-1,cv2.LINE_AA)
        if outline:
            inner = cv2.erode(mask,np.ones((3,3),np.uint8),
                              borderType=cv2.BORDER_CONSTANT,borderValue=0)
            mask = cv2.subtract(mask,inner)
        return self._remember(self._masks,key,mask,64)

    def _blend(self, image, rect, color, alpha, radius=20, outline=False):
        x1,y1,x2,y2 = map(int,rect)
        if x2<=x1 or y2<=y1:
            return
        width,height = x2-x1,y2-y1
        ix1,iy1,ix2,iy2 = max(0,x1),max(0,y1),min(image.shape[1],x2),min(image.shape[0],y2)
        if ix2<=ix1 or iy2<=iy1:
            return
        mask = self._round(width,height,radius,outline)[iy1-y1:iy2-y1,ix1-x1:ix2-x1]
        roi = image[iy1:iy2,ix1:ix2]
        key = roi.shape,tuple(map(int,color))
        if key not in self._solid:
            self._remember(self._solid,key,np.full(roi.shape,color,np.uint8),48)
        solid = self._solid[key]
        if roi.shape not in self._work:
            self._work[roi.shape] = np.empty_like(roi)
        work = self._work[roi.shape]
        cv2.addWeighted(roi,1-float(alpha),solid,float(alpha),0,dst=work)
        cv2.copyTo(work,mask,roi)

    def panel(self, image, rect, color=(49,51,53), alpha=.49, radius=None,
              shadow=True, border=True):
        width,height = rect[2]-rect[0],rect[3]-rect[1]
        radius = min(30,int(min(width,height)*.16)) if radius is None else radius
        if shadow:
            x1,y1,x2,y2 = rect
            # Two cached rounded silhouettes give a soft shadow without blur.
            self._blend(image,(x1-5,y1+5,x2+5,y2+11),(0,0,0),.045,radius+5)
            self._blend(image,(x1-2,y1+3,x2+2,y2+7),(0,0,0),.07,radius+2)
        self._blend(image,rect,color,alpha,radius)
        if border:
            self._blend(image,rect,(232,234,236),.19,radius,outline=True)

    def _text_mask(self, label, size):
        key = str(label),max(8,int(size))
        if key in self._texts:
            self._texts.move_to_end(key)
            return self._texts[key]
        if key[1] not in self._fonts:
            self._fonts[key[1]] = ImageFont.truetype(self.font_path,key[1])
        font = self._fonts[key[1]]
        box = font.getbbox(key[0])
        width,height = max(1,box[2]-box[0]),max(1,box[3]-box[1])
        canvas = Image.new('L',(width,height),0)
        ImageDraw.Draw(canvas).text((-box[0],-box[1]),key[0],font=font,fill=255)
        mask = np.asarray(canvas).copy()
        return self._remember(self._texts,key,mask,256)

    def text(self, image, label, point, size=24, color=(239,241,242), align='left'):
        mask = self._text_mask(label,size)
        height,width = mask.shape
        x,y = map(int,point)
        if align == 'center':
            x -= width//2
        elif align == 'right':
            x -= width
        elif align != 'left':
            raise ValueError('Text alignment must be left, center or right')
        x1,y1,x2,y2 = max(0,x),max(0,y),min(image.shape[1],x+width),min(image.shape[0],y+height)
        if x2>x1 and y2>y1:
            alpha = mask[y1-y:y2-y,x1-x:x2-x].astype(np.float32)[:,:,None]/255.
            roi = image[y1:y2,x1:x2]
            roi[:] = (roi*(1-alpha)+np.asarray(color,dtype=np.float32)*alpha).astype(np.uint8)
        return x,y,x+width,y+height

    @staticmethod
    def _line(image, points, color, thickness, closed=False):
        cv2.polylines(image,[np.rint(points).astype(np.int32)],closed,color,thickness,cv2.LINE_AA)

    def button(self, image, rect, icon, active=False):
        x1,y1,x2,y2 = map(int,rect)
        width,height = x2-x1,y2-y1
        self.panel(image,rect,color=(112,114,116) if active else (67,70,73),
                   alpha=.64 if active else .53,radius=int(min(width,height)*.33),shadow=False)
        cx,cy = (x1+x2)*.5,(y1+y2)*.5
        r = min(width,height)*.23
        thick = max(2,int(min(width,height)*.035))
        color = (249,250,250) if active else (228,232,234)
        line = lambda pts,closed=False: self._line(image,pts,color,thick,closed)
        if icon == 'close':
            line(((cx-r*.7,cy-r*.7),(cx+r*.7,cy+r*.7)))
            line(((cx-r*.7,cy+r*.7),(cx+r*.7,cy-r*.7)))
        elif icon == 'check':
            line(((cx-r,cy),(cx-r*.25,cy+r*.65),(cx+r,cy-r*.75)))
        elif icon in ('undo','back'):
            line(((cx-r*.15,cy-r*.75),(cx-r,cy),(cx-r*.15,cy+r*.75)))
            if icon == 'undo':
                line(((cx-r,cy),(cx+r*.45,cy),(cx+r*.8,cy+r*.35),(cx+r*.8,cy+r*.8)))
            else:
                line(((cx-r,cy),(cx+r,cy)))
        elif icon == 'reset':
            cv2.ellipse(image,(int(cx),int(cy)),(int(r),int(r)),0,35,325,color,thick,cv2.LINE_AA)
            line(((cx+r*.25,cy-r*1.05),(cx+r*.83,cy-r*.67),(cx+r*.95,cy-r*1.25)))
        elif icon == 'replay':
            cv2.circle(image,(int(cx),int(cy)),int(r*1.12),color,thick,cv2.LINE_AA)
            cv2.fillConvexPoly(image,np.rint(((cx-r*.35,cy-r*.62),(cx+r*.65,cy),(cx-r*.35,cy+r*.62))).astype(np.int32),color,cv2.LINE_AA)
        elif icon == 'rotate':
            ellipse_w = min(width*.24,height*.7)
            cv2.ellipse(image,(int(cx),int(cy)),(int(ellipse_w),int(r*.6)),0,20,160,color,thick,cv2.LINE_AA)
            cv2.ellipse(image,(int(cx),int(cy)),(int(ellipse_w),int(r*.6)),0,200,340,color,thick,cv2.LINE_AA)
            line(((cx-ellipse_w*.8,cy-r*.9),(cx-ellipse_w,cy-r*.2),(cx-ellipse_w*.6,cy-r*.1)))
            line(((cx+ellipse_w*.8,cy+r*.9),(cx+ellipse_w,cy+r*.2),(cx+ellipse_w*.6,cy+r*.1)))
            if width>height*2.5:
                span=width*.35
                line(((cx-span+r*.25,cy-r*.3),(cx-span,cy),(cx-span+r*.25,cy+r*.3)))
                line(((cx+span-r*.25,cy-r*.3),(cx+span,cy),(cx+span-r*.25,cy+r*.3)))
        elif icon == 'cube':
            top=(cx,cy-r);left=(cx-r,cy-r*.4);right=(cx+r,cy-r*.4)
            middle=(cx,cy+r*.17);bottom=(cx,cy+r)
            line((top,right,middle,left),True)
            line((left,(cx-r,cy+r*.45),bottom,(cx+r,cy+r*.45),right))
            line((middle,bottom))
        elif icon in ('plus','minus'):
            line(((cx-r*.75,cy),(cx+r*.75,cy)))
            if icon=='plus':
                line(((cx,cy-r*.75),(cx,cy+r*.75)))
        elif icon == 'hand':
            line(((cx-r*.65,cy+r),(cx-r*.9,cy),(cx-r*.45,cy+r*.15),(cx-r*.45,cy-r*.65),(cx-r*.12,cy-r*.85),(cx+r*.15,cy-r),(cx+r*.45,cy-r*.7),(cx+r*.75,cy),(cx+r*.6,cy+r)))
        else:
            raise ValueError('Unknown graphic button: '+str(icon))
        return tuple(rect)

    def palm_hint(self, image, rect, active=False):
        """Non-clickable hand + four arrows: one palm moves the view."""
        x1,y1,x2,y2 = rect
        cx,cy = (x1+x2)*.5,(y1+y2)*.5
        r=(y2-y1)*.28
        color=(231,234,235) if active else (155,161,164)
        self._line(image,[(cx-r*.65,cy+r),(cx-r*.9,cy),(cx-r*.45,cy+r*.15),
                          (cx-r*.45,cy-r*.65),(cx-r*.12,cy-r*.85),(cx+r*.15,cy-r),
                          (cx+r*.45,cy-r*.7),(cx+r*.75,cy),(cx+r*.6,cy+r)],color,2)
        for dx,dy in ((1,0),(-1,0),(0,1),(0,-1)):
            start=(int(cx+dx*r*1.5),int(cy+dy*r*1.5))
            end=(int(cx+dx*r*2.35),int(cy+dy*r*2.35))
            cv2.arrowedLine(image,start,end,color,1,cv2.LINE_AA,tipLength=.38)

    def projection(self, image, rect, mask, differences=(), label='正视图'):
        rows = tuple(tuple(bool(v) for v in row) for row in mask)
        if not rows or not rows[0] or any(len(row)!=len(rows[0]) for row in rows):
            raise ValueError('Projection mask must be nonempty and rectangular')
        self.panel(image,rect,color=(51,54,57),alpha=.30,radius=17,shadow=False)
        x1,y1,x2,y2 = map(int,rect)
        pad = max(9,int(min(x2-x1,y2-y1)*.08))
        label_size = max(13,int(min(x2-x1,y2-y1)*.115))
        self.text(image,label,(x1+pad,y1+pad),size=label_size,color=(226,230,233))
        top = y1+pad+label_size+9
        cell = min((x2-x1-2*pad)/len(rows[0]),(y2-top-pad)/len(rows))
        left = (x1+x2-cell*len(rows[0]))*.5
        differences=set(tuple(d) for d in differences)
        centers={}
        for z,row in enumerate(rows):
            screen_row=len(rows)-1-z
            for col,occupied in enumerate(row):
                px1,py1 = int(round(left+col*cell)),int(round(top+screen_row*cell))
                px2,py2 = int(round(left+(col+1)*cell)),int(round(top+(screen_row+1)*cell))
                color=(173,179,184) if occupied else (68,72,76)
                cv2.rectangle(image,(px1+1,py1+1),(px2-1,py2-1),color,-1)
                cv2.rectangle(image,(px1,py1),(px2,py2),(211,215,218) if occupied else (110,117,122),1,cv2.LINE_AA)
                if (z,col) in differences:
                    cv2.rectangle(image,(px1+1,py1+1),(px2-1,py2-1),(144,157,202),2,cv2.LINE_AA)
                centers[z,col]=((px1+px2)*.5,(py1+py2)*.5)
        return centers
