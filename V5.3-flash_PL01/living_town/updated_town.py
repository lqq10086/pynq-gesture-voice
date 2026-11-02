"""Retained drawing UI and a small, independently timed living-town layer."""
import json
from pathlib import Path
import time

import cv2
import numpy as np

from .sketch_app import SketchTownApp
from .sketch_model import SketchTownModel, MAX_SAVE_BYTES
from .sketch_render import SketchTownRenderer
from .render import TITLES, TownRenderer, _intersect, _union
from .layers import OverlayPatch, PreparedUI
from .living import LivingAnimation, LivingRendererMixin
from .road import snap_car_position


class UpdatedTownModel(SketchTownModel):
    """Two static preset switches, saved separately from the 12 scene objects."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.land_enabled=self.road_enabled=True

    @staticmethod
    def _environment_document(document):
        if isinstance(document,dict) and document.get('version')==4:
            environment=document.get('environment')
            if (not isinstance(environment,dict) or set(environment)!={'land','road'}
                    or any(type(v) is not bool for v in environment.values())):
                raise ValueError('invalid terrain preset switches')
            original=dict(document);original.pop('environment');original['version']=3
            return original,environment
        return document,dict(land=True,road=True)

    def snapshot(self):
        document=super().snapshot();document['version']=4
        document['environment']=dict(land=self.land_enabled,road=self.road_enabled)
        return document

    def _validated_sketch_document(self,document):
        original,_=self._environment_document(document)
        # Old works used arbitrary ground cells for cars. Migrate only their
        # placement to the lane; preserve IDs, drawings and all other objects.
        if isinstance(original,dict) and isinstance(original.get('instances'),list):
            original=dict(original);items=[]
            for entry in original['instances']:
                if isinstance(entry,dict) and entry.get('category')=='car':
                    entry=dict(entry)
                    entry['x'],entry['y']=snap_car_position(entry.get('x'),entry.get('y'),entry.get('size_index',1))
                items.append(entry)
            original['instances']=items
        return super()._validated_sketch_document(original)

    def _fit_car(self,item):
        if item.category=='car':
            item.x,item.y=snap_car_position(item.x,item.y,item.size_index)
            self.road_enabled=True
        return item

    def add_instance(self,*args,**kwargs):
        return self._fit_car(super().add_instance(*args,**kwargs))

    def move(self,identifier,x,y):
        item=self.get_instance(identifier)
        if item.category!='car':return super().move(identifier,x,y)
        position=snap_car_position(x,y,item.size_index)
        if (item.x,item.y)!=position:
            item.x,item.y=position;self.scene_revision+=1
        self.road_enabled=True
        return item

    def copy_instance(self,identifier):
        return self._fit_car(super().copy_instance(identifier))

    def load(self,path):
        path=Path(path)
        if path.stat().st_size>MAX_SAVE_BYTES:raise ValueError('work exceeds the save size limit')
        document=json.loads(path.read_bytes().decode('utf-8'))
        _,environment=self._environment_document(document)
        super().load(path)
        self.land_enabled,self.road_enabled=environment['land'],environment['road']
        for item in self.instances:self._fit_car(item)
        return self


class UpdatedTownRenderer(LivingRendererMixin, SketchTownRenderer):
    # A slightly deeper veil still retains 65% of the CURRENT camera.
    FILM_ALPHA=89
    FILM_BGR=(180,180,180)
    INK_COLOR=(15,15,15)
    BOARD_COLOR=FILM_BGR
    IDLE_TERRAIN_ALPHA=140

    def __init__(self,width,height,root=None,font_path=None):
        base=Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.terrain=json.loads((base/'assets/terrain/presets.json').read_text(encoding='utf-8'))
        self._land_enabled=self._road_enabled=True
        self._terrain_opaque=False
        self._environment_key=(True,True,False)
        self._edit_inverse=None
        super().__init__(width,height,root=root,font_path=font_path)
        # Floating buttons use the existing cached font/icon bodies, opaque.
        self.ui=self._edit_ui

    def _layout(self):
        super()._layout()
        self.layout['land']=self._r(92,602,184,656)
        self.layout['road']=self._r(194,602,286,656)
        self.layout['copy']=self._r(492,618,556,678)
        self.layout['delete']=self._r(574,618,638,678)
        self.layout['play']=self._r(860,618,924,678)
        self.layout['replay']=self._r(942,618,1006,678)
        self.layout['clear']=self._r(820,550,896,632)
        self.layout['back']=self._r(922,550,998,632)
        self.layout['submit']=self._r(1024,550,1130,632)

    def _init_projection(self):
        super()._init_projection()
        factor=float(self.terrain['wall_height_factor'])
        far=self._plane_origins['ground']
        rise=(self._plane_origins['background']-far)*factor
        self.wall_height_world*=factor
        self._plane_origins['background']=far+rise
        self._plane_matrices['background'][:,1]=-rise
        self._plane_inverse['background']=np.linalg.inv(self._plane_matrices['background'])
        self._depth_coefficients['background']=(8*self.camera[0],-self.wall_height_world*self.camera[2],
            6*self.camera[1]+self.wall_height_world*self.camera[2])
        points=[self.plane_to_screen(x,y,'background') for x,y in ((0,0),(1,0),(1,1),(0,1))]
        self.layout['scene_background']=(int(np.floor(min(p[0] for p in points))),int(np.floor(min(p[1] for p in points))),
            int(np.ceil(max(p[0] for p in points))),int(np.ceil(max(p[1] for p in points))))

    def _library_rects(self,state):
        return {'library_'+str(b.id):self._r(92,112+i*48,286,156+i*48)
                for i,b in enumerate(state.model.library[:8])}

    def _sync_layout(self,state):
        top=112+48*min(8,len(state.model.library))
        self.layout['new']=self._r(92,top,286,top+54)

    def _button_names(self,state):
        if state.stage=='edit':return ('close','clear','back','submit')
        return ('close','new','copy','delete','land','road','play','replay')

    def _make_room(self):
        """Rasterize a few fixed polygons/lines once, with no translucent slabs."""
        c=self.layout['scene_canvas']
        image=np.zeros((c[3]-c[1],c[2]-c[0],4),np.uint8)
        def points(values,plane='ground'):
            return np.rint([(self.plane_to_screen(x,y,plane)[0]-c[0],self.plane_to_screen(x,y,plane)[1]-c[1])
                           for x,y in values]).astype(np.int32)
        def rectangle(rect,color):
            x1,y1,x2,y2=rect
            alpha=255 if self._terrain_opaque else self.IDLE_TERRAIN_ALPHA
            cv2.fillConvexPoly(image,points(((x1,y1),(x2,y1),(x2,y2),(x1,y2))),tuple(color)+(alpha,),cv2.LINE_AA)
        def line(a,b,color,plane='ground',thickness=1):
            p=points((a,b),plane)
            cv2.line(image,tuple(p[0]),tuple(p[1]),tuple(color)+(255,),max(1,int(round(thickness*self.s))),cv2.LINE_AA)
        if self._land_enabled:
            land=self.terrain['land'];rectangle(land['rect'],land['bgr'])
            if not hasattr(self,'_land_texture'):
                rng=np.random.default_rng(7)
                coarse=cv2.resize(rng.normal(0,2.4,(24,32)).astype(np.float32),(512,384),interpolation=cv2.INTER_CUBIC)
                grain=rng.normal(0,1.2,(384,512)).astype(np.float32)
                color=np.asarray(land['bgr'],np.float32)
                texture=np.clip(color+(coarse+grain)[:,:,None],0,255).astype(np.uint8)
                for _ in range(140):
                    point=tuple(int(v) for v in rng.integers((0,0),(512,384)))
                    shade=tuple(int(v) for v in np.clip(color+int(rng.choice((-9,7))),0,255))
                    cv2.ellipse(texture,point,(1,1),0,0,360,shade,-1,cv2.LINE_AA)
                self._land_texture=texture
            x1,y1,x2,y2=land['rect']
            source=np.float32(((0,0),(511,0),(0,383)))
            target=points(((x1,y1),(x2,y1),(x1,y2))).astype(np.float32)
            material=cv2.warpAffine(self._land_texture,cv2.getAffineTransform(source,target),
                (image.shape[1],image.shape[0]),flags=cv2.INTER_LINEAR,borderMode=cv2.BORDER_REFLECT_101)
            mask=image[:,:,3]>0
            image[:,:,:3][mask]=material[mask]
        if self._road_enabled:
            road=self.terrain['road'];x1,y1,x2,y2=road['rect']
            rectangle((x1,y1-.025,x2,y1),road['curb_bgr'])
            rectangle((x1,y2,x2,min(1.,y2+.01)),road['curb_bgr'])
            rectangle(road['rect'],road['bgr'])
            for x in np.linspace(.08,.86,7):
                rectangle((float(x),road['center_y']-.004,float(x)+.055,road['center_y']+.004),road['mark_bgr'])
        if not self._terrain_opaque:
            ground=self.terrain['ground_grid_bgr'];sky=self.terrain['sky_grid_bgr']
            for col in range(9):line((col/8,0),(col/8,1),sky,'background')
            for row in range(5):line((0,row/4),(1,row/4),sky,'background')
            for col in range(9):line((col/8,0),(col/8,1),ground)
            for row in range(7):line((0,row/6),(1,row/6),ground)
        # The room buffer uses the original premultiplied RGBA interface.
        image[:,:,:3]=np.rint(image[:,:,:3]*image[:,:,3:4].astype(np.float32)/255.).astype(np.uint8)
        return image

    def _ensure_environment(self,state):
        key=(bool(state.model.land_enabled),bool(state.model.road_enabled),bool(state.animation.playing))
        if key!=self._environment_key:
            self._environment_key=key
            self._land_enabled,self._road_enabled,self._terrain_opaque=key
            self._scene_background=self._make_room();self._scene_initialized=False
            self._prepared_key=self._ui_key=None

    def visual_key(self,state,now):
        key=super().visual_key(state,now)
        if state.stage=='scene':return key+(bool(state.model.land_enabled),bool(state.model.road_enabled),state.animation.playing)
        return key

    def presentation_guard_key(self,state,now):
        """A newer animation pose must not invalidate a completed UI packet.

        Interaction, editing and timeout changes still invalidate it. Picking
        uses the prepared records, so a slow board can show an older complete
        pose while continuously preparing the next one without starvation.
        """
        key=self.visual_key(state,now)
        if state.stage!='scene':return key
        fields=list(key);fields[4]=TownRenderer._object_records(self,state)
        return tuple(fields)+(state.animation.started,tuple(sorted(state.animation.edited_ids)))

    def _static_draw(self,image,state):
        # Original scene buttons, but no enclosing panel, shadows or glass.
        if state.stage!='scene':return
        for name,rect in self._library_rects(state).items():
            bp=next(b for b in state.model.library if 'library_'+str(b.id)==name)
            self.ui.panel(image,rect,(75,75,75))
            sprite=self._sprite(bp.category,role='thumb')
            self._alpha(image,sprite,rect[0]+int(9*self.s),int((rect[1]+rect[3]-sprite.shape[0])*.5))
            self.ui.text(image,TITLES[bp.category],(rect[0]+int(75*self.s),rect[1]+int(11*self.s)),max(12,int(17*self.s)),(237,237,237))
        self._button(image,'new','plus')
        for name,icon in (('copy',None),('delete',None)):
            self._button(image,name,icon)
        for name in ('play','replay'):
            rect=self.layout[name];self.ui.panel(image,rect,(75,75,75))
            cx,cy=(rect[0]+rect[2])//2,(rect[1]+rect[3])//2
            radius=max(6,int(12*self.s));color=(237,237,237);width=max(2,int(2*self.s))
            if name=='play' and state.animation.playing:
                for offset in (-5,5):
                    cv2.line(image,(cx+int(offset*self.s),cy-radius),(cx+int(offset*self.s),cy+radius),color,width+1,cv2.LINE_AA)
            elif name=='play':
                cv2.fillConvexPoly(image,np.array(((cx-radius//2,cy-radius),(cx-radius//2,cy+radius),(cx+radius,cy)),np.int32),color,cv2.LINE_AA)
            else:
                cv2.ellipse(image,(cx,cy),(radius,radius),0,35,320,color,width,cv2.LINE_AA)
                tip=(cx+int(radius*.77),cy-int(radius*.64))
                cv2.polylines(image,[np.array(((tip[0]-int(8*self.s),tip[1]),tip,(tip[0],tip[1]-int(8*self.s))),np.int32)],False,color,width,cv2.LINE_AA)
        for name,title,enabled in (('land','地面',state.model.land_enabled),('road','道路',state.model.road_enabled)):
            rect=self.layout[name]
            self.ui.panel(image,rect,(110,110,110) if enabled else (61,61,61))
            self.ui.text(image,title,((rect[0]+rect[2])//2,rect[1]+int(15*self.s)),max(12,int(19*self.s)),(238,238,238),'center')

    @staticmethod
    def _write_foreground(base,inverse,pixels,left,top):
        h,w=pixels.shape[:2];mask=cv2.inRange(pixels,(1,1,1),(255,255,255))
        cv2.copyTo(pixels,mask,base[top:top+h,left:left+w])
        target=inverse[top:top+h,left:left+w];target[mask>0]=0

    def _reset_ink_area(self):
        l,t,r,b=self._local_rect(self.layout['board'])
        premult=np.rint(np.asarray(self.FILM_BGR)*self.FILM_ALPHA/255.).astype(np.uint8)
        self._edit_base[t:b,l:r]=premult
        self._edit_inverse[t:b,l:r]=255-self.FILM_ALPHA

    def _static_editor(self,state,now):
        p=self.layout['panel'];shape=(p[3]-p[1],p[2]-p[0],3)
        if self._edit_base is None:self._edit_base=np.empty(shape,np.uint8)
        if self._edit_inverse is None:self._edit_inverse=np.empty(shape,np.uint8)
        self._edit_base.fill(0);self._edit_inverse.fill(255)
        self._reset_ink_area()
        board=self._local_rect(self.layout['board'])
        cv2.rectangle(self._edit_base,(board[0]-1,board[1]-1),(board[2],board[3]),(105,105,105),1,cv2.LINE_8)
        cv2.rectangle(self._edit_inverse,(board[0]-1,board[1]-1),(board[2],board[3]),(0,0,0),1,cv2.LINE_8)
        ui=self._edit_ui
        def control(rect,draw):
            local=self._local_rect(rect);pixels=np.zeros((local[3]-local[1],local[2]-local[0],3),np.uint8)
            draw(pixels,(0,0,pixels.shape[1],pixels.shape[0]))
            self._write_foreground(self._edit_base,self._edit_inverse,pixels,local[0],local[1])
        for name,icon in (('back','undo'),('submit','check')):
            control(self.layout[name],lambda image,rect,icon=icon:ui.button(image,rect,icon))
        def trash(image,rect):
            ui.panel(image,rect);cx,cy=rect[2]//2,rect[3]//2;r=max(5,int(12*self.s))
            color=(235,235,235);thick=max(1,int(2*self.s))
            cv2.rectangle(image,(cx-r,cy-r+3),(cx+r,cy+r),color,thick,cv2.LINE_AA)
            cv2.line(image,(cx-r-3,cy-r),(cx+r+3,cy-r),color,thick,cv2.LINE_AA)
            cv2.line(image,(cx-r//2,cy-r-4),(cx+r//2,cy-r-4),color,thick,cv2.LINE_AA)
        control(self.layout['clear'],trash)
        category=self._live_category(state)
        if category in TITLES:
            sprite=self._sprite(category,role='preview');rect=self._local_rect(self.layout['preview'])
            left=int((rect[0]+rect[2]-sprite.shape[1])*.5);top=int((rect[1]+rect[3]-sprite.shape[0])*.5)
            self._alpha(self._edit_base,sprite,left,top)
            self._edit_inverse[top:top+sprite.shape[0],left:left+sprite.shape[1]]=255-sprite[:,:,3:4]
        feedback=self._feedback(state,now)
        if feedback:
            mask=ui._text_mask(feedback,max(10,int(17*self.s)))
            pixels=np.zeros((mask.shape[0],mask.shape[1],3),np.uint8)
            pixels[mask>0]=(45,45,45)
            x,y=self._local_point(self._p(980,510));x-=pixels.shape[1]//2
            self._write_foreground(self._edit_base,self._edit_inverse,pixels,x,y)
        self._edit_counts['full_board_rebuilds']+=1

    def _segment(self,a,b):
        aa,bb=self._ink_point(a),self._ink_point(b)
        thickness=max(2,int(round(3*self.s)))
        l,t,r,bottom=self._local_rect(self.layout['board'])
        start,end=(aa[0]-l,aa[1]-t),(bb[0]-l,bb[1]-t)
        for canvas,color in ((self._edit_base,self.INK_COLOR),(self._edit_inverse,(0,0,0))):
            ink=canvas[t:bottom,l:r]
            if start==end:cv2.circle(ink,start,max(1,thickness//2),color,-1,cv2.LINE_AA)
            else:cv2.line(ink,start,end,color,thickness,cv2.LINE_AA)
        self._edit_counts['ink_segment_draws']+=1
        px,py,_,_=self.layout['panel'];pad=thickness+2
        return _intersect((min(aa[0],bb[0])+px-pad,min(aa[1],bb[1])+py-pad,
            max(aa[0],bb[0])+px+pad+1,max(aa[1],bb[1])+py+pad+1),self.layout['board'])

    def _update_ink(self,strokes,rebuild=False):
        old=self._edit_strokes
        append_only=(not rebuild and len(strokes)>=len(old) and (not old or
            (strokes[:len(old)-1]==old[:-1] and strokes[len(old)-1][:len(old[-1])]==old[-1])))
        if not append_only:
            self._reset_ink_area();dirty=self.layout['board']
            for stroke in strokes:self._draw_stroke(stroke)
            self._edit_counts['ink_rebuilds']+=1
        else:
            dirty=None
            if old:dirty=self._draw_stroke(strokes[len(old)-1],len(old[-1]))
            for stroke in strokes[len(old):]:dirty=_union(dirty,self._draw_stroke(stroke))
        self._edit_strokes=strokes
        return dirty

    def _brush_on_tile(self,pixels,inverse,rect,point):
        if point is None:return
        area=_intersect(rect,self.layout['board'])
        if area is None:return
        x,y=point[0]-area[0],point[1]-area[1]
        local=(area[1]-rect[1],area[3]-rect[1],area[0]-rect[0],area[2]-rect[0])
        scale=self.s;tip=(int(x),int(y));neck=(int(x+4*scale),int(y-6*scale));handle=(int(x+10*scale),int(y-14*scale))
        for canvas,body,point_color in ((pixels,(55,55,55),(15,15,15)),(inverse,(0,0,0),(0,0,0))):
            part=canvas[local[0]:local[1],local[2]:local[3]]
            cv2.line(part,neck,handle,body,max(2,int(4*scale)),cv2.LINE_AA)
            cv2.line(part,tip,neck,point_color,max(1,int(2*scale)),cv2.LINE_AA)
            cv2.circle(part,tip,max(1,int(2*scale)),point_color,-1,cv2.LINE_AA)

    @staticmethod
    def _compact(patch,tile=192):
        x0,y0,x2,y2=patch.rect;result=[]
        active=np.any(patch.inverse!=255,axis=2)|np.any(patch.premult!=0,axis=2)
        active=active.astype(np.uint8)*255
        for y in range(0,y2-y0,tile):
            for x in range(0,x2-x0,tile):
                part=active[y:y+tile,x:x+tile];bx,by,bw,bh=cv2.boundingRect(part)
                if not bw or not bh:continue
                xx,yy=x+bx,y+by
                if xx==0 and yy==0 and bw==x2-x0 and bh==y2-y0:
                    result.append(patch)
                    continue
                result.append(OverlayPatch((x0+xx,y0+yy,x0+xx+bw,y0+yy+bh),
                    patch.premult[yy:yy+bh,xx:xx+bw],patch.inverse[yy:yy+bh,xx:xx+bw]))
        return tuple(result)

    def _prepare_scene(self,state,now,revision):
        """Retain static controls and publish only changed scene tiles.

        All buffers contain camera-independent coefficients. The display owner
        can therefore composite the last valid packet onto a NEW video frame.
        """
        self._ensure_environment(state)
        key=self.visual_key(state,now)
        if key==getattr(self,'_scene_packet_key',None):
            return self._revision(self._scene_packet,revision,now)
        started=time.perf_counter();self._sync_layout(state)
        buttons=(tuple((b.id,b.category,b.variant) for b in state.model.library),
                 state.model.land_enabled,state.model.road_enabled,state.animation.playing,
                 self._feedback(state,now))
        if buttons!=getattr(self,'_scene_button_key',None):
            self._build_layer(state,buttons)
            self._scene_controls=self._compact(OverlayPatch(self._ui_rect,*self._ui_layer))
            self._scene_button_key=buttons
        self._draw_scene(state)
        regions=self._last_scene_dirty_regions
        c=self.layout['scene_canvas'];height,width=self._scene.shape[:2]
        if not hasattr(self,'_scene_tiles'):
            self._scene_tiles={};regions=((0,0,width,height),)
        changed=0
        if regions:
            for y in range(0,height,192):
                for x in range(0,width,192):
                    local=(x,y,min(x+192,width),min(y+192,height))
                    if not any(_intersect(local,dirty) is not None for dirty in regions):continue
                    rect=(c[0]+x,c[1]+y,c[0]+local[2],c[1]+local[3])
                    patch=OverlayPatch(rect,self._scene[y:local[3],x:local[2],:3],
                                       self._scene_inverse[y:local[3],x:local[2]])
                    self._scene_tiles[(x,y)]=self._compact(patch);changed+=1
        if self._edit_close is None:
            rect=self.layout['close'];pixels=np.zeros((rect[3]-rect[1],rect[2]-rect[0],3),np.uint8)
            self._edit_ui.button(pixels,(0,0,pixels.shape[1],pixels.shape[0]),'close')
            inverse=np.repeat(np.where(np.any(pixels>0,axis=2),0,255).astype(np.uint8)[:,:,None],3,axis=2)
            self._edit_close=OverlayPatch(rect,pixels,inverse)
        patches=list(self._scene_controls)
        for tiles in self._scene_tiles.values():patches.extend(tiles)
        patches.append(self._edit_close)
        self._scene_packet=PreparedUI(int(revision),float(now),self.width,self.height,tuple(patches),())
        self._scene_packet_key=key
        self._counts['last_scene_dirty_tiles']=changed
        self._counts['scene_preparations']=self._counts.get('scene_preparations',0)+1
        self._counts['last_prepare_ms']=(time.perf_counter()-started)*1000.
        return self._scene_packet

    def prepare(self,state,now,revision):
        if state.stage!='edit':
            return self._prepare_scene(state,now,revision)
        key=self.visual_key(state,now)
        if key==self._edit_key:
            self._edit_counts['prepare_cache_hits']+=1
            return self._revision(self._edit_prepared,revision,now)
        started=time.perf_counter();brush=self._brush(state)
        static=(self._live_category(state),self._feedback(state,now))
        rebuilt=self._edit_base is None or static!=self._edit_static_key
        if rebuilt:
            self._static_editor(state,now);self._edit_static_key=static;self._new_tiles()
            self._edit_ready=set();self._edit_compact={}
        ink_revision=state.model.sketch_revision
        if rebuilt or ink_revision!=self._edit_ink_revision:
            dirty=self._update_ink(self._strokes(state),rebuilt);self._edit_ink_revision=ink_revision
        else:dirty=None
        regions=[r for r in (dirty,self._brush_rect(self._edit_brush),self._brush_rect(brush)) if r is not None]
        changed=0;p=self.layout['panel']
        for i,rect in enumerate(self._edit_rects):
            if not rebuilt and i in self._edit_ready and not any(_intersect(rect,r) for r in regions):continue
            l,t,r,b=rect[0]-p[0],rect[1]-p[1],rect[2]-p[0],rect[3]-p[1]
            pixels=self._edit_base[t:b,l:r].copy();inverse=self._edit_inverse[t:b,l:r].copy()
            self._brush_on_tile(pixels,inverse,rect,brush)
            self._edit_tiles[i]=(None if np.all(inverse==255) and not np.any(pixels)
                                 else OverlayPatch(rect,pixels,inverse))
            self._edit_ready.add(i);changed+=1
        if self._edit_close is None:
            rect=self.layout['close'];pixels=np.zeros((rect[3]-rect[1],rect[2]-rect[0],3),np.uint8)
            self._edit_ui.button(pixels,(0,0,pixels.shape[1],pixels.shape[0]),'close')
            inverse=np.repeat(np.where(np.any(pixels>0,axis=2),0,255).astype(np.uint8)[:,:,None],3,axis=2)
            self._edit_close=OverlayPatch(rect,pixels,inverse)
        # Crop empty areas once on each changed tile; unchanged tile objects are
        # retained to avoid preparation work when only the small brush moves.
        if not hasattr(self,'_edit_compact'):self._edit_compact={}
        parts=[]
        for tile in self._edit_tiles:
            if tile is None:continue
            entry=self._edit_compact.get(tile.rect)
            if entry is None or entry[0] is not tile:
                entry=(tile,self._compact(tile));self._edit_compact[tile.rect]=entry
            parts.extend(entry[1])
        parts.append(self._edit_close)
        self._edit_prepared=PreparedUI(int(revision),float(now),self.width,self.height,tuple(parts),())
        self._edit_key=key;self._edit_brush=brush
        self._edit_counts['prepare_count']+=1;self._edit_counts['tile_publications']+=changed
        self._edit_counts['last_dirty_tiles']=changed
        self._edit_counts['last_prepare_ms']=(time.perf_counter()-started)*1000.
        return self._edit_prepared

    def render(self,state,bgr,now):
        # Both pages use the same prepared layers as the independent display.
        packet=self.prepare(state,now,0)
        if self._edit_compositor is None:
            from .layers import DisplayCompositor
            self._edit_compositor=DisplayCompositor(self.width,self.height,'tint')
        np.copyto(self._output,bgr);self._edit_compositor.compose_into(self._output,packet)
        return self._output

    def metrics(self):
        result=super().metrics()
        arrays=[a for a in (self._edit_base,self._edit_inverse) if a is not None]
        packets=[p for p in self._edit_tiles if p is not None]
        packets.extend(getattr(self,'_scene_controls',()))
        for tile in getattr(self,'_scene_tiles',{}).values():packets.extend(tile)
        for entry in getattr(self,'_edit_compact',{}).values():packets.extend(entry[1])
        if self._edit_close is not None:packets.append(self._edit_close)
        for patch in packets:
            arrays.extend((patch.premult,patch.inverse))
            if patch.copy_mask is not None:arrays.append(patch.copy_mask)
        ui_bytes=sum(a.nbytes for a in {id(a):a for a in arrays}.values())
        result.update(glass_regions=0,transparent_planes=not self._terrain_opaque,sky_height_factor=self.terrain['wall_height_factor'],
            grid_only_planes=not self._terrain_opaque,placement_grid_visible=not self._terrain_opaque,
            terrain_presets=dict(land=self._land_enabled,road=self._road_enabled),
            writing_film_alpha=self.FILM_ALPHA/255.,writing_film_bgr=list(self.FILM_BGR),ink_bgr=list(self.INK_COLOR))
        result['terrain_opacity']=1. if self._terrain_opaque else self.IDLE_TERRAIN_ALPHA/255.
        result['sketch_editor']['opaque_blackboard']=False
        result['sketch_editor']['interval_modes_ms']=[]
        result['sketch_editor']['owned_ui_cache_bytes']=ui_bytes
        result['living']=dict(self.living_metrics(),refresh_limit_hz=LivingAnimation.HZ,static_controls_retained=True,dirty_scene_tiles=True)
        return result


class UpdatedTownApp(SketchTownApp):
    def __init__(self,width=1280,height=720,font_path=None,root=None,work_path=None,model=None,renderer=None):
        root=Path(root) if root is not None else Path(__file__).resolve().parents[1]
        super().__init__(width,height,font_path=font_path,root=root,work_path=work_path,
            model=model if model is not None else UpdatedTownModel(root=root),
            renderer=renderer if renderer is not None else UpdatedTownRenderer(width,height,root=root,font_path=font_path))
        self.animation=LivingAnimation()
        self._animation_now=0.

    @property
    def metrics(self):
        result=super().metrics
        result['animation']=dict(playing=self.animation.playing,elapsed=self.animation.elapsed,
                                car_finished=self.animation.car_finished)
        return result

    def tick(self,now):
        self._animation_now=float(now)
        events=super().tick(now)
        if self.stage=='scene':self.animation.tick(now)
        return events

    def _switch(self,stage):
        if hasattr(self,'animation') and stage!='scene':self.animation.pause(self._animation_now)
        super()._switch(stage)

    def _start_drag(self,object_id,point):
        # An edited object is never moved by animation at the same time.
        self.renderer.release_for_edit(self,object_id)
        self.animation.pause(self._animation_now)
        super()._start_drag(object_id,point)

    def _move_drag(self,point,recovered=False):
        super()._move_drag(point,recovered)
        if self.drag_preview is not None:
            identifier,x,y=self.drag_preview
            item=self.model.get_instance(identifier)
            if item.category=='car':
                x,y=snap_car_position(x,y,item.size_index)
                self.drag_preview=(identifier,x,y)

    def _button(self,target,now):
        self._animation_now=float(now)
        if self.stage=='scene' and target in ('play','replay'):
            if target=='play':self.animation.play_pause(now)
            else:self.animation.replay(now)
            return True
        if self.stage=='scene' and target in ('land','road'):
            # Cars keep a visible road; hiding the route would defeat the
            # placement constraint even though the coordinates remain valid.
            if target=='road' and self.model.road_enabled and any(o.category=='car' for o in self.model.instances):
                return True
            name=target+'_enabled';setattr(self.model,name,not getattr(self.model,name))
            self.model.scene_revision+=1
            return True
        return super()._button(target,now)
