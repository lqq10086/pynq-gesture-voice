"""Small wall-clock animation over the retained, camera-free town layers.

Only the first authored car, at most two clouds and three chimney puffs move.
The original object coordinates stay intact: animation is a display override.
All sprite/shadow preparation happens once per bounded size/frame key.
"""
import math
from pathlib import Path

import cv2
import numpy as np

from .render import _inside, _intersect, _union
from .road import CAR_ANCHOR, TYRE_CONTACTS, clamp_car_position, car_route


class LivingAnimation:
    HZ = 30.
    CAR_SECONDS = 14.
    ROAD_START,ROAD_END=car_route()

    def __init__(self):
        self.playing = self.started = False
        self.elapsed = 0.
        self._last_now = None
        self.edited_ids = set()

    @property
    def visual_time(self):
        return math.floor((self.elapsed + 1e-9) * self.HZ) / self.HZ

    @property
    def car_finished(self):
        return self.started and self.visual_time >= self.CAR_SECONDS

    def tick(self, now):
        now = float(now)
        old = self.visual_time
        if self.playing and self._last_now is not None:
            self.elapsed += max(0., now - self._last_now)
        self._last_now = now
        return self.visual_time != old

    def play_pause(self, now):
        if self.playing:
            self.pause(now)
        else:
            self.started = self.playing = True
            self._last_now = float(now)

    def pause(self, now):
        self.tick(now)
        self.playing = False

    def replay(self, now):
        self.elapsed = 0.
        self.edited_ids.clear()
        self._last_now = float(now)
        self.started = self.playing = True

    def car_position(self,size_index=1):
        phase = min(1., self.visual_time / self.CAR_SECONDS)
        start,end=car_route(size_index)
        if phase>=1.:
            return end
        return tuple(a + (b-a)*phase for a,b in zip(start,end))

    def key(self):
        return self.started,self.playing,self.visual_time


def _merged_regions(regions):
    """Merge touching regions, without one large car-to-sky bounding rectangle."""
    result=[]
    for rect in regions:
        if rect is None:
            continue
        pending=rect
        i=0
        while i<len(result):
            previous=result[i]
            if (pending[0]<=previous[2] and previous[0]<=pending[2]
                    and pending[1]<=previous[3] and previous[1]<=pending[3]):
                pending=_union(pending,result.pop(i));i=0
            else:
                i+=1
        result.append(pending)
    return tuple(result)


class LivingRendererMixin:
    """Mixin for UpdatedTownRenderer; reuse its scene, sprites and projection.

    The UI owner calls app.tick regardless of detector output. _object_records
    observes only the controller's quantized time; painting never advances it.
    """
    def _living_assets(self):
        if hasattr(self,'_living_smoke'):
            return
        directory=Path(self.root)/'assets'/'town'/'smoke'
        self._living_smoke=[]
        for index in range(12):
            pixels=cv2.imdecode(np.fromfile(str(directory/('%02d.png'%index)),np.uint8),cv2.IMREAD_UNCHANGED)
            if pixels is None or pixels.shape[2]!=4:
                raise ValueError('Missing precomputed chimney puff frame')
            self._living_smoke.append(pixels)
        self._living_scaled={}
        self._living_shadow_offsets={}
        self._living_coefficients={}
        self._living_edges={}
        self._living_work=np.empty_like(self._scene_work)
        canvas=self.layout['scene_canvas'];ground=np.zeros(self._scene.shape[:2],np.uint8)
        points=np.rint([(self.plane_to_screen(x,y)[0]-canvas[0],self.plane_to_screen(x,y)[1]-canvas[1])
                       for x,y in ((0,0),(1,0),(1,1),(0,1))]).astype(np.int32)
        cv2.fillConvexPoly(ground,points,255)
        self._living_ground_mask=ground.astype(bool)
        self._living_counts=dict(dirty_updates=0,dirty_pixels=0,smoke_frames_loaded=12,
                                 shadow_preparations=0,sprite_preparations=0)

    def _shadow_sprite(self,category,size_index):
        """Cache close contact shadows; only clouds cast a distant silhouette."""
        source=super()._sprite(category,size_index)
        height,width=source.shape[:2]
        anchor=np.asarray(CAR_ANCHOR if category=='car' else (.5,.5) if category=='cloud' else (.5,1.))
        anchor*=width,height
        if category!='cloud':
            padding=max(4,int(round(4*self.s)))
            alpha=np.zeros((height+2*padding,width+2*padding),np.uint8)
            opaque=source[:,:,3]>=128
            if category=='car':
                for tx,ty in TYRE_CONTACTS:
                    # Locate each tyre's actual lowest visible pixels rather
                    # than projecting the body into a detached cast shadow.
                    x1=max(0,int((tx-.055)*width));x2=min(width,int((tx+.055)*width)+1)
                    y1=max(0,int((ty-.095)*height));y2=min(height,int((ty+.095)*height)+1)
                    ys,xs=np.nonzero(opaque[y1:y2,x1:x2])
                    bottom=ys.max();cx=float(xs[ys==bottom].mean())+x1;cy=float(bottom+y1)+.5
                    center=(round(cx)+padding,round(cy)+padding)
                    cv2.ellipse(alpha,center,(max(3,round(width*.045)),max(1,round(self.s))),
                                0,0,360,100,-1,cv2.LINE_AA)
            elif category=='tree':
                ys,xs=np.nonzero(opaque)
                bottom=ys.max();roots=xs[ys>=bottom-max(1,int(height*.025))]
                center=(round(float(roots.mean()))+padding,int(bottom)+padding)
                cv2.ellipse(alpha,center,(max(4,round(width*.12)),max(1,round(2*self.s))),
                            0,0,360,62,-1,cv2.LINE_AA)
            else:
                # The lower alpha envelope follows both visible foundation
                # walls, including their corner, with only a narrow soft seam.
                edge=[]
                for x in range(width):
                    rows=np.flatnonzero(opaque[:,x])
                    if len(rows) and rows[-1]>=height*.66:
                        edge.append((x+padding,int(rows[-1])+padding))
                cv2.polylines(alpha,[np.asarray(edge,np.int32)],False,44,
                              max(1,round(self.s)),cv2.LINE_AA)
            alpha=cv2.GaussianBlur(alpha,(0,0),max(.65,1.*self.s))
            ys,xs=np.nonzero(alpha)
            left,top,right,bottom=int(xs.min()),int(ys.min()),int(xs.max())+1,int(ys.max())+1
            alpha=alpha[top:bottom,left:right]
            pixels=np.empty(alpha.shape+(4,),np.uint8)
            pixels[:,:,:3]=(29,32,35);pixels[:,:,3]=alpha
            self._living_shadow_offsets[(category,int(size_index))]=tuple(np.rint(-anchor-padding+(left,top)).astype(int))
            self._living_counts['shadow_preparations']+=1
            return pixels
        matrix=self._plane_matrices['ground']
        gx,gy=(matrix[:,index]/np.linalg.norm(matrix[:,index]) for index in (0,1))
        axes=np.column_stack((gx*.92,gy*.45))
        corners=np.asarray(((0,0),(width,0),(width,height),(0,height)),np.float64)
        projected=(corners-anchor)@axes.T
        sigma=max(.8,5.5*self.s)
        margin=int(math.ceil(sigma*3))+2
        low=np.floor(projected.min(axis=0))-margin
        high=np.ceil(projected.max(axis=0))+margin
        shape=tuple((high-low).astype(int))
        affine=np.column_stack((axes,-axes@anchor-low))
        alpha=cv2.warpAffine(source[:,:,3],affine,shape,flags=cv2.INTER_LINEAR)
        alpha=cv2.GaussianBlur(alpha,(0,0),sigma)
        alpha=np.rint(alpha*.13).astype(np.uint8)
        pixels=np.empty((shape[1],shape[0],4),np.uint8)
        pixels[:,:,:3]=(29,32,35);pixels[:,:,3]=alpha
        self._living_shadow_offsets[(category,int(size_index))]=tuple(low.astype(int))
        self._living_counts['shadow_preparations']+=1
        return pixels

    def _sprite(self,category,size_index=1,role='scene'):
        if not category.startswith('__living_'):
            return super()._sprite(category,size_index,role)
        self._living_assets()
        key=category,int(size_index)
        if key in self._living_scaled:
            return self._living_scaled[key]
        if category=='__living_shadow':
            pixels=self._shadow_sprite('car',size_index)
        elif category.startswith('__living_cast:'):
            pixels=self._shadow_sprite(category.split(':',1)[1],size_index)
        elif category.startswith('__living_smoke:'):
            original=self._living_smoke[int(size_index)]
            pixels=cv2.resize(original,(max(8,int(31*self.s)),max(8,int(31*self.s))),interpolation=cv2.INTER_LINEAR)
        elif category=='__living_cloud':
            pixels=super()._sprite('cloud',1)
        else:
            raise ValueError('Unknown living-town sprite')
        self._living_scaled[key]=pixels
        self._living_counts['sprite_preparations']+=1
        return pixels

    def _object_records(self,state):
        base=list(super()._object_records(state))
        animation=getattr(state,'animation',None)
        started=animation is not None and animation.started
        t=animation.visual_time if started else 0.
        car=next((record[0] for record in base if record[1]=='car'),None)
        house=next((record for record in base if record[1]=='house'),None)
        clouds=[record[0] for record in base if record[1]=='cloud'][:2]
        preview=getattr(state,'drag_preview',None)
        records=[]
        for record in base:
            identifier,category,x,y,size,variant,plane=record
            if category=='car':
                if (started and identifier==car and identifier not in animation.edited_ids
                        and not (preview is not None and preview[0]==identifier)):
                    x,y=animation.car_position(size)
                x,y=clamp_car_position(x,y,size)
                records.append(('__living_shadow:'+identifier, '__living_shadow',x,y,size,'default','ground'))
            elif (started and identifier in clouds and identifier not in animation.edited_ids
                    and not (preview is not None and preview[0]==identifier)):
                index=clouds.index(identifier)
                # Continuous bounded drift: no wrapping or sudden teleport.
                x+=.035*(math.sin(t*.28+index*1.9)-math.sin(index*1.9))
                y+=.012*(math.sin(t*.20+index)-math.sin(index))
                x,y=min(.94,max(.06,x)),min(.94,max(.06,y))
            if category in ('tree','house'):
                records.append(('__living_cast:'+identifier,'__living_cast:'+category,x,y,size,'default','ground'))
            elif category=='cloud':
                records.append(self._cloud_shadow(identifier,x,y,size))
            records.append((identifier,category,x,y,size,variant,plane))
        for index in range(len(clouds),2) if started else ():
            phase=index*1.7
            x=(.27,.72)[index]+.045*math.sin(t*.26+phase)
            y=(.20,.31)[index]+.014*math.sin(t*.18+phase)
            records.append(self._cloud_shadow('__living_cloud%d'%index,x,y,1))
            records.append(('__living_cloud%d'%index,'__living_cloud',x,y,1,'default','background'))
        if started and house is not None:
            # Three tiny, pre-authored alpha frames; zero-alpha endpoints make
            # the life-cycle reset invisible. The anchor is the real chimney.
            for index in range(3):
                phase=(t/3.+index/3.)%1.
                frame=min(11,int(phase*12))
                records.append(('__living_smoke%d'%index,'__living_smoke:%d'%house[4],
                                house[2],house[3],frame,'default','ground'))
        return tuple(sorted(records,key=lambda r:(-1 if r[1]=='__living_shadow' or r[1].startswith('__living_cast:') else 0 if r[6]=='background' else 1,
            self.camera_depth(r[2],r[3],r[6]))))

    def _cloud_shadow(self,identifier,x,y,size):
        height=self.wall_height_world*(1.-y)
        return ('__living_cast:'+identifier,'__living_cast:cloud',x+.25*height/8.,
                .70*height/6.,size,'default','ground')

    def release_for_edit(self,state,object_id):
        """Transfer an animated item to the authored grid before hand dragging.

        A drop remains grid-snapped, as in the original app. Replay explicitly
        re-enables its scripted path; pause/continue never overrides an edit.
        """
        animation=getattr(state,'animation',None)
        if animation is None or not animation.started:
            return
        records=self._scene_records if self._scene_initialized else self._object_records(state)
        record=next((r for r in records if r[0]==object_id),None)
        if record is None:
            return
        animation.edited_ids.add(object_id)
        state.model.move(object_id,record[2],record[3])

    def _object_rect(self,record,local=False):
        category=record[1]
        if category=='car':
            sprite=self._sprite('car',record[4])
            x,y=self.plane_to_screen(record[2],record[3],record[6])
            if local:
                c=self.layout['scene_canvas'];x,y=x-c[0],y-c[1]
            left=int(round(x-sprite.shape[1]*CAR_ANCHOR[0]))
            top=int(round(y-sprite.shape[0]*CAR_ANCHOR[1]))
            return left,top,left+sprite.shape[1],top+sprite.shape[0]
        if not category.startswith('__living_'):
            return super()._object_rect(record,local)
        sprite=self._sprite(category,record[4])
        x,y=self.plane_to_screen(record[2],record[3],record[6])
        if category=='__living_shadow' or category.startswith('__living_cast:'):
            source='car' if category=='__living_shadow' else category.split(':',1)[1]
            dx,dy=self._living_shadow_offsets[(source,int(record[4]))]
            left,top=int(round(x+dx)),int(round(y+dy))
        elif category.startswith('__living_smoke:'):
            house_size=int(category.split(':')[1]);house=super()._sprite('house',house_size)
            phase=record[4]/11.
            x=x-house.shape[1]*.5+house.shape[1]*.51+phase*7*self.s
            y=y-house.shape[0]+house.shape[0]*.025-phase*34*self.s
            left,top=int(round(x-sprite.shape[1]*.5)),int(round(y-sprite.shape[0]*.92))
        else:
            left,top=int(round(x-sprite.shape[1]*.5)),int(round(y-sprite.shape[0]*.5))
        if local:
            canvas=self.layout['scene_canvas'];left,top=left-canvas[0],top-canvas[1]
        return left,top,left+sprite.shape[1],top+sprite.shape[0]

    def pick_scene(self,point,state):
        if not _inside(point,self.layout['scene_canvas']):
            return None
        records=self._scene_records if self._scene_initialized else self._object_records(state)
        for record in reversed(records):
            # Decoration and contact shadows never steal object selections.
            if record[0].startswith('__living_'):
                continue
            rect=self._object_rect(record)
            if _inside(point,rect):
                sprite=self._sprite(record[1],record[4]);x,y=int(point[0]-rect[0]),int(point[1]-rect[1])
                if sprite[y,x,3]>=32:
                    return record[0]
        return None

    def _living_over(self,sprite,left,top,clip,ground_only=False):
        """Cached byte coefficients avoid repeated float conversion per frame."""
        self._living_assets()
        key=id(sprite)
        entry=self._living_coefficients.get(key)
        if entry is None or entry[0] is not sprite:
            alpha=sprite[:,:,3:4]
            premult=cv2.multiply(sprite[:,:,:3],np.repeat(alpha,3,axis=2),scale=1/255.,dtype=cv2.CV_8U)
            inverse=np.repeat(255-alpha,3,axis=2)
            entry=(sprite,premult,inverse)
            # The source sprites themselves have a bounded24-entry cache.
            # Limit old coefficient owners too, including the12 smoke frames.
            if len(self._living_coefficients)>=48:
                self._living_coefficients.pop(next(iter(self._living_coefficients)))
            self._living_coefficients[key]=entry
        rect=(left,top,left+sprite.shape[1],top+sprite.shape[0])
        area=_intersect(rect,clip)
        if area is None:
            return
        x1,y1,x2,y2=area
        sy,sx=slice(y1-top,y2-top),slice(x1-left,x2-left)
        target=self._scene[y1:y2,x1:x2]
        rgb=target[:,:,:3]
        cv2.multiply(rgb,entry[2][sy,sx],dst=self._scene_work[y1:y2,x1:x2],scale=1/255.,dtype=cv2.CV_8U)
        cv2.add(self._scene_work[y1:y2,x1:x2],entry[1][sy,sx],dst=self._living_work[y1:y2,x1:x2])
        mask=self._living_ground_mask[y1:y2,x1:x2,None] if ground_only else True
        np.copyto(rgb,self._living_work[y1:y2,x1:x2],where=mask)
        # Existing scene storage is premultiplied; alpha-over stays byte-native.
        old=target[:,:,3:4]
        remaining=cv2.multiply(old,entry[2][sy,sx,:1],scale=1/255.,dtype=cv2.CV_8U)
        np.copyto(target[:,:,3:4],cv2.add(remaining,sprite[sy,sx,3:4]).reshape(old.shape),where=mask)

    def _draw_scene(self,state):
        records=self._object_records(state);selected=getattr(state,'selected_id',None)
        if self._scene_initialized and records==self._scene_records and selected==self._scene_selected:
            self._last_scene_dirty=None;self._last_scene_dirty_regions=()
            return
        bounds=(0,0,self._scene.shape[1],self._scene.shape[0])
        if not self._scene_initialized:
            regions=(bounds,);self._counts['scene_rebuilds']+=1
        else:
            old={r[0]:r for r in self._scene_records};new={r[0]:r for r in records}
            affected={k for k in set(old)|set(new) if old.get(k)!=new.get(k)}
            if selected!=self._scene_selected:
                affected.update((selected,self._scene_selected))
            rectangles=[]
            for identifier in affected:
                for mapping in (old,new):
                    if identifier in mapping:
                        rect=self._object_rect(mapping[identifier],True)
                        rectangles.append(_intersect((rect[0]-3,rect[1]-3,rect[2]+3,rect[3]+3),bounds))
            regions=_merged_regions(rectangles)
            self._counts['scene_dirty_updates']+=1
        self._living_assets()
        for dirty in regions:
            x1,y1,x2,y2=dirty
            self._scene[y1:y2,x1:x2]=self._scene_background[y1:y2,x1:x2]
            for record in records:
                rect=self._object_rect(record,True)
                if _intersect(rect,dirty) is None:
                    continue
                sprite=self._sprite(record[1],record[4])
                shadow=record[1]=='__living_shadow' or record[1].startswith('__living_cast:')
                self._living_over(sprite,rect[0],rect[1],dirty,ground_only=shadow)
                if record[0]==selected:
                    entry=self._living_edges.get(id(sprite))
                    if entry is None or entry[0] is not sprite:
                        edge=np.empty_like(sprite);edge[:,:,:3]=(247,250,250)
                        edge[:,:,3]=cv2.subtract(cv2.dilate(sprite[:,:,3],np.ones((3,3),np.uint8)),
                                                cv2.erode(sprite[:,:,3],np.ones((3,3),np.uint8)))
                        if len(self._living_edges)>=24:
                            self._living_edges.pop(next(iter(self._living_edges)))
                        entry=(sprite,edge);self._living_edges[id(sprite)]=entry
                    self._living_over(entry[1],rect[0],rect[1],dirty)
            self._scene_inverse[y1:y2,x1:x2]=255-self._scene[y1:y2,x1:x2,3:4]
            area=(x2-x1)*(y2-y1)
            self._counts['scene_dirty_pixels']+=area
            self._living_counts['dirty_pixels']+=area
        self._living_counts['dirty_updates']+=bool(regions)
        self._last_scene_dirty_regions=regions
        self._last_scene_dirty=None
        for rect in regions:
            self._last_scene_dirty=_union(self._last_scene_dirty,rect)
        self._scene_records,self._scene_selected=records,selected
        self._scene_initialized=True;self._draw_order=[r[0] for r in records]

    def living_metrics(self):
        self._living_assets()
        arrays=list(self._living_smoke)+list(self._living_scaled.values())+[self._living_work]
        arrays.extend(a for entry in self._living_coefficients.values() for a in entry[1:])
        arrays.extend(entry[1] for entry in self._living_edges.values())
        return dict(self._living_counts,animation_hz=LivingAnimation.HZ,
                    max_dynamic_clouds=2,max_smoke_puffs=3,
                    cache_bytes=sum(a.nbytes for a in {id(a):a for a in arrays}.values()),
                    coefficient_cache_entries=len(self._living_coefficients),
                    dirty_regions=[list(rect) for rect in getattr(self,'_last_scene_dirty_regions',())])
