"""Opaque drawing board with incremental ink and cached tile publications.

The existing TownRenderer still owns scene projection, sprites and placement.
Only the editor is replaced. It never classifies a drawing or reads a camera.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from spatial_build.ui import Ui
from .layers import DisplayCompositor, OverlayPatch, PreparedUI
from .render import TownRenderer, TITLES, _inside, _intersect, _union


class _OpaqueUi(Ui):
    """Retain cached text/icons but draw button bodies without alpha/shadows."""
    def panel(self, image, rect, color=(69, 72, 70), alpha=1., radius=None,
              shadow=False, border=False):
        x1,y1,x2,y2=map(int,rect)
        ix1,iy1,ix2,iy2=max(0,x1),max(0,y1),min(image.shape[1],x2),min(image.shape[0],y2)
        if ix2<=ix1 or iy2<=iy1:return
        w,h=x2-x1,y2-y1
        radius=min(15,int(min(w,h)*.22)) if radius is None else radius
        mask=self._round(w,h,radius)[iy1-y1:iy2-y1,ix1-x1:ix2-x1]
        gray=int(round(sum(color)/3))
        key=((iy2-iy1,ix2-ix1,3),(gray,gray,gray))
        if key not in self._solid:
            self._remember(self._solid,key,np.full(key[0],gray,np.uint8),48)
        cv2.copyTo(self._solid[key],mask,image[iy1:iy2,ix1:ix2])


class SketchTownRenderer(TownRenderer):
    LOGICAL_SIZE=(640.,480.)
    TILE_SIZE=192
    BOARD_COLOR=(17,21,19)
    INK_COLOR=(231,235,229)

    def __init__(self,width,height,root=None,font_path=None):
        super().__init__(width,height,root=root,font_path=font_path)
        self._edit_ui=_OpaqueUi(width,height,font_path)
        self._edit_base=None
        self._edit_static_key=None
        self._edit_strokes=()
        self._edit_ink_revision=None
        self._edit_brush=None
        self._edit_key=None
        self._edit_prepared=None
        self._edit_tiles=[]
        self._edit_rects=[]
        self._edit_close=None
        self._edit_compositor=None
        self._edit_counts=dict(full_board_rebuilds=0,ink_rebuilds=0,
            ink_segment_draws=0,tile_publications=0,prepare_count=0,
            prepare_cache_hits=0,last_dirty_tiles=0)

    def _layout(self):
        super()._layout()
        self.layout['board']=self._r(104,130,744,610)
        self.layout['preview']=self._r(804,184,1148,494)

    def screen_to_sketch(self,point):
        if point is None or not _inside(point,self.layout['board']):return None
        l,t,r,b=self.layout['board']
        return ((float(point[0])-l)*640./(r-l),(float(point[1])-t)*480./(b-t))

    def logical_to_screen(self,point):
        l,t,r,b=self.layout['board']
        return l+float(point[0])*(r-l)/640.,t+float(point[1])*(b-t)/480.

    def _button_names(self,state):
        if state.stage=='edit':
            return ('close','clear','submit','back')
        return super()._button_names(state)

    def hit_test(self,point,state):
        if state.stage!='edit':return super().hit_test(point,state)
        for name in self._button_names(state):
            if _inside(point,self.layout[name]):return 'button',name
        return ('drag','paint') if _inside(point,self.layout['board']) else ('empty',)

    @staticmethod
    def _strokes(state):
        # SketchTownModel owns validated immutable tuples. Retain that reference
        # instead of repacking up to 1024 points during cursor-only updates.
        strokes=getattr(state.model,'strokes',())
        return strokes if isinstance(strokes,tuple) else tuple(tuple(line) for line in strokes)

    def _brush(self,state):
        point=getattr(state,'brush_point',None)
        if point is None or not _inside(point,self.layout['board']):return None
        return tuple(int(round(v)) for v in point)

    @staticmethod
    def _feedback(state,now):
        return str(getattr(state,'feedback','')) if now<float(getattr(state,'feedback_until',0.)) else ''

    def visual_key(self,state,now):
        if state.stage!='edit':return super().visual_key(state,now)
        return ('sketch_edit',int(getattr(state.model,'sketch_revision',0)),
                self._brush(state),
                self._live_category(state),self._feedback(state,now))

    def _local_rect(self,rect):
        px,py,_,_=self.layout['panel']
        return rect[0]-px,rect[1]-py,rect[2]-px,rect[3]-py

    def _local_point(self,point):
        px,py,_,_=self.layout['panel'];return int(round(point[0]-px)),int(round(point[1]-py))

    def _static_editor(self,state,now):
        p=self.layout['panel'];self._edit_base=np.full((p[3]-p[1],p[2]-p[0],3),self.BOARD_COLOR,np.uint8)
        ui=self._edit_ui
        board=self._local_rect(self.layout['board'])
        cv2.rectangle(self._edit_base,(board[0]-1,board[1]-1),(board[2],board[3]),
                      (87,98,91),max(1,int(self.s)),cv2.LINE_8)
        for name,icon in (('back','undo'),('submit','check')):
            ui.button(self._edit_base,self._local_rect(self.layout[name]),icon)
        # Trash means clear; the hooked arrow returns to the town.
        rect=self._local_rect(self.layout['clear']);ui.panel(self._edit_base,rect)
        cx,cy=(rect[0]+rect[2])//2,(rect[1]+rect[3])//2;r=max(5,int(12*self.s))
        color=(232,237,233);thick=max(1,int(2*self.s))
        cv2.rectangle(self._edit_base,(cx-r,cy-r+3),(cx+r,cy+r),color,thick,cv2.LINE_AA)
        cv2.line(self._edit_base,(cx-r-3,cy-r),(cx+r+3,cy-r),color,thick,cv2.LINE_AA)
        cv2.line(self._edit_base,(cx-r//2,cy-r-4),(cx+r//2,cy-r-4),color,thick,cv2.LINE_AA)
        category=self._live_category(state)
        if category in TITLES:
            sprite=self._sprite(category,role='preview');rect=self._local_rect(self.layout['preview'])
            self._alpha(self._edit_base,sprite,int((rect[0]+rect[2]-sprite.shape[1])*.5),int((rect[1]+rect[3]-sprite.shape[0])*.5))
            ui.text(self._edit_base,TITLES[category],((rect[0]+rect[2])//2,rect[3]-max(16,int(28*self.s))),
                    max(12,int(20*self.s)),(231,237,232),'center')
        feedback=self._feedback(state,now)
        if feedback:
            x,y=self._local_point(self._p(980,510))
            ui.text(self._edit_base,feedback,(x,y),max(10,int(17*self.s)),(212,221,215),'center')
        self._edit_counts['full_board_rebuilds']+=1

    def _ink_point(self,point):
        x,y=self.logical_to_screen(point);l,t,r,b=self.layout['board']
        return self._local_point((min(r-1,max(l,x)),min(b-1,max(t,y))))

    def _segment(self,a,b):
        aa,bb=self._ink_point(a),self._ink_point(b)
        thickness=max(2,int(round(3*self.s)))
        l,t,r,bottom=self._local_rect(self.layout['board'])
        ink=self._edit_base[t:bottom,l:r]
        start,end=(aa[0]-l,aa[1]-t),(bb[0]-l,bb[1]-t)
        if start==end:cv2.circle(ink,start,max(1,thickness//2),self.INK_COLOR,-1,cv2.LINE_AA)
        else:cv2.line(ink,start,end,self.INK_COLOR,thickness,cv2.LINE_AA)
        self._edit_counts['ink_segment_draws']+=1
        px,py,_,_=self.layout['panel'];pad=thickness+2
        return _intersect((min(aa[0],bb[0])+px-pad,min(aa[1],bb[1])+py-pad,
                max(aa[0],bb[0])+px+pad+1,max(aa[1],bb[1])+py+pad+1),self.layout['board'])

    def _draw_stroke(self,stroke,start=0):
        dirty=None
        if not stroke:return dirty
        if start==0:dirty=self._segment(stroke[0],stroke[0]);start=1
        for i in range(max(1,start),len(stroke)):
            dirty=_union(dirty,self._segment(stroke[i-1],stroke[i]))
        return dirty

    def _update_ink(self,strokes,rebuild=False):
        old=self._edit_strokes
        append_only=(not rebuild and len(strokes)>=len(old)
            and (not old or (strokes[:len(old)-1]==old[:-1]
                 and strokes[len(old)-1][:len(old[-1])]==old[-1])))
        if not append_only:
            l,t,r,b=self._local_rect(self.layout['board'])
            self._edit_base[t:b,l:r]=self.BOARD_COLOR
            dirty=self.layout['board']
            for stroke in strokes:self._draw_stroke(stroke)
            self._edit_counts['ink_rebuilds']+=1
        else:
            dirty=None
            if old:
                dirty=self._draw_stroke(strokes[len(old)-1],len(old[-1]))
            for stroke in strokes[len(old):]:dirty=_union(dirty,self._draw_stroke(stroke))
        self._edit_strokes=strokes
        return dirty

    def _brush_rect(self,point):
        if point is None:return None
        radius=max(10,int(16*self.s));return point[0]-radius,point[1]-radius,point[0]+radius+1,point[1]+radius+1

    def _draw_brush(self,image,tile_rect,point):
        if point is None:return
        area=_intersect(tile_rect,self.layout['board'])
        if area is None:return
        image=image[area[1]-tile_rect[1]:area[3]-tile_rect[1],area[0]-tile_rect[0]:area[2]-tile_rect[0]]
        x,y=point[0]-area[0],point[1]-area[1]
        scale=self.s;thick=max(1,int(2*scale));white=(224,236,225)
        tip=(int(x),int(y));neck=(int(x+4*scale),int(y-6*scale));handle=(int(x+10*scale),int(y-14*scale))
        cv2.line(image,neck,handle,(164,190,170),max(2,int(4*scale)),cv2.LINE_AA)
        cv2.line(image,tip,neck,white,thick,cv2.LINE_AA)
        cv2.circle(image,tip,max(1,int(2*scale)),(244,248,244),-1,cv2.LINE_AA)

    def _new_tiles(self):
        p=self.layout['panel'];self._edit_rects=[]
        tile=max(64,int(round(self.TILE_SIZE*self.s)))
        for y in range(p[1],p[3],tile):
            for x in range(p[0],p[2],tile):
                self._edit_rects.append((x,y,min(p[2],x+tile),min(p[3],y+tile)))
        self._edit_tiles=[None]*len(self._edit_rects)

    @staticmethod
    def _revision(packet,revision,now):
        if packet.revision==int(revision):return packet
        return PreparedUI(int(revision),float(now),packet.width,packet.height,packet.patches,packet.glass_regions)

    def prepare(self,state,now,revision):
        if state.stage!='edit':
            return self._revision(super().prepare(state,now,revision),revision,now)
        key=self.visual_key(state,now)
        if key==self._edit_key:
            self._edit_counts['prepare_cache_hits']+=1
            return self._revision(self._edit_prepared,revision,now)
        started=time.perf_counter();brush=self._brush(state)
        ink_revision=getattr(state.model,'sketch_revision',None)
        ink_changed=ink_revision is None or ink_revision!=self._edit_ink_revision
        strokes=self._strokes(state) if ink_changed else self._edit_strokes
        static_key=(self._live_category(state),self._feedback(state,now))
        rebuilt=self._edit_base is None or static_key!=self._edit_static_key
        if rebuilt:
            self._static_editor(state,now);self._edit_static_key=static_key
            self._new_tiles()
        dirty=self._update_ink(strokes,rebuild=rebuilt) if ink_changed or rebuilt else None
        self._edit_ink_revision=ink_revision
        dirty_regions=[r for r in (dirty,self._brush_rect(self._edit_brush),self._brush_rect(brush)) if r is not None]
        changed=0;p=self.layout['panel']
        for i,rect in enumerate(self._edit_rects):
            if not rebuilt and self._edit_tiles[i] is not None and not any(_intersect(rect,r) is not None for r in dirty_regions):continue
            l,t,r,b=rect[0]-p[0],rect[1]-p[1],rect[2]-p[0],rect[3]-p[1]
            pixels=self._edit_base[t:b,l:r].copy()
            self._draw_brush(pixels,rect,brush)
            self._edit_tiles[i]=OverlayPatch(rect,pixels,np.zeros_like(pixels));changed+=1
        if self._edit_close is None:
            rect=self.layout['close'];pixels=np.zeros((rect[3]-rect[1],rect[2]-rect[0],3),np.uint8)
            self._edit_ui.button(pixels,(0,0,pixels.shape[1],pixels.shape[0]),'close')
            # Keep rounded-button corners open to the live camera; its body is
            # still opaque and uses the same binary-mask copy fast path.
            inverse=np.repeat(np.where(np.any(pixels>0,axis=2),0,255).astype(np.uint8)[:,:,None],3,axis=2)
            self._edit_close=OverlayPatch(rect,pixels,inverse)
        patches=tuple(self._edit_tiles)+(self._edit_close,)
        self._edit_prepared=PreparedUI(int(revision),float(now),self.width,self.height,patches,())
        self._edit_key=key;self._edit_brush=brush
        self._edit_counts['prepare_count']+=1;self._edit_counts['tile_publications']+=changed
        self._edit_counts['last_dirty_tiles']=changed
        self._edit_counts['last_prepare_ms']=(time.perf_counter()-started)*1000.
        return self._edit_prepared

    def render(self,state,bgr,now):
        if state.stage!='edit':return super().render(state,bgr,now)
        if bgr.dtype!=np.uint8 or bgr.shape!=self._output.shape:raise ValueError('Expected a uint8 BGR frame with renderer dimensions')
        packet=self.prepare(state,now,0)
        if self._edit_compositor is None:self._edit_compositor=DisplayCompositor(self.width,self.height,'tint')
        np.copyto(self._output,bgr)
        self._edit_compositor.compose_into(self._output,packet)
        self._counts['draw_count']+=1
        return self._output

    def metrics(self):
        result=super().metrics()
        extra=0 if self._edit_base is None else self._edit_base.nbytes
        extra+=sum(p.premult.nbytes+p.inverse.nbytes+(getattr(p,'copy_mask',None).nbytes if getattr(p,'copy_mask',None) is not None else 0)
                   for p in self._edit_tiles if p is not None)
        result.update(sketch_editor=dict(self._edit_counts,opaque_blackboard=True,
            logical_size=[640,480],interval_modes_ms=[],tile_count=len(self._edit_tiles),
            extra_buffer_bytes=extra,classification_in_renderer=False))
        return result
