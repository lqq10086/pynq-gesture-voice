"""Cached transparent grid-room compositor; screen coordinates are pixels.

Only changed local layers are rasterised. Camera pixels remain live beneath
both planes. No terrain, blur, screenshot recognition, or extra AI is used.
"""
from collections import OrderedDict
from pathlib import Path
import time
import cv2
import numpy as np
from spatial_build.ui import Ui
from spatial_build.renderer import camera_basis

TITLES={'tree':'树','house':'房子','car':'车辆','cloud':'云'}

def _inside(p,r):return r[0]<=p[0]<r[2] and r[1]<=p[1]<r[3]

def _union(a,b):
    if a is None:return b
    if b is None:return a
    return min(a[0],b[0]),min(a[1],b[1]),max(a[2],b[2]),max(a[3],b[3])

def _intersect(a,b):
    r=max(a[0],b[0]),max(a[1],b[1]),min(a[2],b[2]),min(a[3],b[3])
    return r if r[2]>r[0] and r[3]>r[1] else None

class TownRenderer:
    def __init__(self,width,height,root=None,font_path=None):
        self.width,self.height=int(width),int(height)
        if min(self.width,self.height)<240:raise ValueError('Town rendering needs at least a 240-pixel surface')
        self.root=Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.ui=Ui(width,height,font_path)
        self.s=min(width/1280.,height/720.);self.ox=(width-1280*self.s)*.5;self.oy=(height-720*self.s)*.5
        self.layout={};self._layout();self._init_projection()
        self._output=np.empty((height,width,3),np.uint8);self._sprites={};self._scaled=OrderedDict()
        self._counts=dict(decode_count=0,resize_count=0,scene_rebuilds=0,scene_dirty_updates=0,scene_dirty_pixels=0,ui_rebuilds=0,draw_count=0)
        for category in TITLES:
            filename=category+'_v54.png' if category in ('car','tree','cloud') else category+'.png'
            path=self.root/'assets'/'town'/filename
            if not path.is_file():raise FileNotFoundError('Missing required transparent town asset: '+str(path))
            rgba=cv2.imdecode(np.fromfile(str(path),np.uint8),cv2.IMREAD_UNCHANGED)
            if rgba is None or rgba.ndim!=3 or rgba.shape[2]!=4:raise ValueError('Town asset must be a transparent four-channel PNG: '+str(path))
            ys,xs=np.nonzero(rgba[:,:,3]>4)
            if not len(xs) or np.all(rgba[:,:,3]==255):raise ValueError('Town asset must contain visible pixels and transparent background: '+str(path))
            self._sprites[category]=np.ascontiguousarray(rgba[ys.min():ys.max()+1,xs.min():xs.max()+1]);self._counts['decode_count']+=1
        self._ui_key=None;self._ui_layer=None;self._ui_rect=self.layout['panel'];self._blend_work=None
        self._scene_background=self._make_room();self._scene=self._scene_background.copy()
        self._scene_inverse=np.empty(self._scene.shape[:2]+(3,),np.uint8);self._scene_work=np.empty_like(self._scene_inverse)
        self._scene_records=();self._scene_selected=None;self._scene_initialized=False;self._draw_order=[]

    def _p(self,x,y):return int(round(self.ox+x*self.s)),int(round(self.oy+y*self.s))
    def _r(self,a,b,c,d):return self._p(a,b)+self._p(c,d)

    def _layout(self):
        self.layout.update(panel=self._r(64,87,1216,686),close=self._r(613,24,667,78),board=self._r(122,170,584,632),
            scene_canvas=self._r(304,87,1203,606),scene_ground=self._r(340,389,1192,592),
            scene_background=self._r(432,158,1100,389),preview=self._r(720,184,1110,493))
        buttons=dict(new=(92,120,286,182),clear=(820,550,894,632),
            submit=(1030,550,1140,632),back=(925,550,999,632),
            copy=(515,618,575,674),delete=(610,618,670,674))
        for name,rect in buttons.items():self.layout[name]=self._r(*rect)

    def cell_center(self,x,y):
        l,t,r,b=self.layout['board'];return l+(int(x)+.5)*(r-l)/7,t+(6-int(y)+.5)*(b-t)/7

    def _init_projection(self):
        """The retained SpatialApp viewing angles, fitted once to two planes.

        Ground save coordinates stay x=left-to-right and y=toward the viewer.
        No runtime 3D camera or polygon engine is introduced by these matrices.
        """
        self.camera_yaw_deg,self.camera_pitch_deg=35.,28.
        self.right,self.up,self.camera=camera_basis(self.camera_yaw_deg,self.camera_pitch_deg)
        self.scene_unit=72*self.s
        self.wall_height_world=105/(72*self.up[2])
        origin=np.asarray(self._p(386,440),np.float64)
        project=np.array([self.right,-self.up],np.float64)*self.scene_unit
        gx=project[:,0]*8;gy=project[:,1]*-6
        far_origin=origin+project[:,1]*6
        wall_up=project[:,2]*self.wall_height_world
        self._plane_origins={'ground':far_origin,'background':far_origin+wall_up}
        self._plane_matrices={'ground':np.column_stack((gx,gy)),
                              'background':np.column_stack((gx,-wall_up))}
        self._plane_inverse={name:np.linalg.inv(matrix) for name,matrix in self._plane_matrices.items()}
        self._depth_coefficients={'ground':(8*self.camera[0],-6*self.camera[1],6*self.camera[1]),
            'background':(8*self.camera[0],-self.wall_height_world*self.camera[2],6*self.camera[1]+self.wall_height_world*self.camera[2])}
        for plane,name in (('ground','scene_ground'),('background','scene_background')):
            points=[self.plane_to_screen(x,y,plane) for x,y in ((0,0),(1,0),(1,1),(0,1))]
            self.layout[name]=(int(np.floor(min(p[0] for p in points))),int(np.floor(min(p[1] for p in points))),
                               int(np.ceil(max(p[0] for p in points))),int(np.ceil(max(p[1] for p in points))))

    def plane_to_screen(self,x,y,plane='ground'):
        if plane not in self._plane_matrices:raise ValueError('Unknown scene plane: '+str(plane))
        x,y=float(x),float(y);m=self._plane_matrices[plane];o=self._plane_origins[plane]
        return float(o[0]+m[0,0]*x+m[0,1]*y),float(o[1]+m[1,0]*x+m[1,1]*y)

    def screen_to_plane(self,point,plane='ground'):
        if plane not in self._plane_inverse:raise ValueError('Unknown scene plane: '+str(plane))
        o=self._plane_origins[plane];px,py=float(point[0])-o[0],float(point[1])-o[1];m=self._plane_inverse[plane]
        return float(m[0,0]*px+m[0,1]*py),float(m[1,0]*px+m[1,1]*py)

    def camera_depth(self,x,y,plane='ground'):
        if plane not in self._depth_coefficients:raise ValueError('Unknown scene plane: '+str(plane))
        a,b,c=self._depth_coefficients[plane];return float(a*float(x)+b*float(y)+c)

    def ground_to_screen(self,x,y):return self.plane_to_screen(x,y,'ground')
    def screen_to_ground(self,point):return self.screen_to_plane(point,'ground')

    def _library_rects(self,state):
        return {'library_'+str(b.id):self._r(92,112+i*58,286,166+i*58) for i,b in enumerate(state.model.library[:8])}

    def _sync_layout(self,state):
        top=120+58*min(8,len(state.model.library))
        self.layout['new']=self._r(92,top,286,top+62)

    def _button_names(self,state):
        if state.stage=='edit':return ('close','clear','submit','back')
        return ('close','new','copy','delete')

    def hit_test(self,point,state):
        self._sync_layout(state)
        for name in self._button_names(state):
            if _inside(point,self.layout[name]):return 'button',name
        if state.stage=='edit' and _inside(point,self.layout['board']):
            l,t,r,b=self.layout['board'];return 'cell',min(6,int((point[0]-l)*7/(r-l))),6-min(6,int((point[1]-t)*7/(b-t)))
        if state.stage=='scene':
            for name,rect in self._library_rects(state).items():
                if _inside(point,rect):return 'button',name
            identifier=self.pick_scene(point,state)
            if identifier is not None:return 'object',identifier
        return 'empty',

    def _sprite(self,category,size_index=1,role='scene'):
        key=category,int(size_index),role
        if key in self._scaled:self._scaled.move_to_end(key);return self._scaled[key]
        source=self._sprites[category]
        if role=='thumb':bounds=47*self.s,36*self.s
        elif role=='preview':bounds=302*self.s,262*self.s
        else:
            factor=(.78,1.,1.22)[max(0,min(2,int(size_index)))];bounds={'tree':(108,172),'house':(150,168),'car':(118,100),'cloud':(128,59)}[category]
            bounds=bounds[0]*self.s*factor,bounds[1]*self.s*factor
        # Preserve the authored sprite proportions within the cached bounds.
        virtual_width=source.shape[1]
        scale=min(bounds[0]/virtual_width,bounds[1]/source.shape[0])
        size=max(1,int(round(virtual_width*scale))),max(1,int(round(source.shape[0]*scale)))
        sprite=cv2.resize(source,size,interpolation=cv2.INTER_AREA if scale<1 else cv2.INTER_LINEAR)
        self._scaled[key]=sprite;self._counts['resize_count']+=1
        while len(self._scaled)>24:self._scaled.popitem(last=False)
        return sprite

    @staticmethod
    def _alpha(image,sprite,left,top,clip=None,opacity=1.):
        rect=int(left),int(top),int(left)+sprite.shape[1],int(top)+sprite.shape[0];area=_intersect(rect,(0,0,image.shape[1],image.shape[0]))
        if clip is not None and area is not None:area=_intersect(area,clip)
        if area is None:return
        x1,y1,x2,y2=area;src=sprite[y1-rect[1]:y2-rect[1],x1-rect[0]:x2-rect[0]];alpha=src[:,:,3:4].astype(np.float32)*(float(opacity)/255.)
        dst=image[y1:y2,x1:x2];dst[:]=np.rint(dst*(1-alpha)+src[:,:,:3]*alpha).astype(np.uint8)

    @staticmethod
    def _over_premult(image,sprite,left,top,clip=None):
        rect=int(left),int(top),int(left)+sprite.shape[1],int(top)+sprite.shape[0];area=_intersect(rect,(0,0,image.shape[1],image.shape[0]))
        if clip is not None and area is not None:area=_intersect(area,clip)
        if area is None:return
        x1,y1,x2,y2=area;src=sprite[y1-rect[1]:y2-rect[1],x1-rect[0]:x2-rect[0]];alpha=src[:,:,3:4].astype(np.float32)/255.;dst=image[y1:y2,x1:x2]
        dst[:,:,:3]=np.rint(src[:,:,:3]*alpha+dst[:,:,:3]*(1-alpha)).astype(np.uint8)
        dst[:,:,3:4]=np.rint(src[:,:,3:4]+dst[:,:,3:4]*(1-alpha)).astype(np.uint8)

    def _object_records(self,state):
        preview=getattr(state,'drag_preview',None);records=[]
        for obj in state.model.instances:
            x,y=float(obj.x),float(obj.y)
            if preview is not None and preview[0]==obj.id:x,y=preview[1],preview[2]
            plane=getattr(obj,'plane','background' if obj.category=='cloud' else 'ground')
            records.append((obj.id,obj.category,x,y,int(obj.size_index),obj.variant,plane))
        # Equal-depth objects follow creation/save order, so the newest item
        # remains accessible at the shared spawn row. UUIDs carry no depth.
        return tuple(sorted(records,key=lambda r:(0 if r[6]=='background' else 1,self.camera_depth(r[2],r[3],r[6]))))

    def _object_rect(self,record,local=False):
        sprite=self._sprite(record[1],record[4]);x,y=self.plane_to_screen(record[2],record[3],record[6])
        if local:c=self.layout['scene_canvas'];x,y=x-c[0],y-c[1]
        left=int(round(x-sprite.shape[1]*.5));top=int(round(y-sprite.shape[0]*(.5 if record[6]=='background' else 1.)))
        return left,top,left+sprite.shape[1],top+sprite.shape[0]

    def pick_scene(self,point,state):
        if not _inside(point,self.layout['scene_canvas']):return None
        for record in reversed(self._object_records(state)):
            rect=self._object_rect(record)
            if _inside(point,rect):
                sprite=self._sprite(record[1],record[4]);x,y=int(point[0]-rect[0]),int(point[1]-rect[1])
                if sprite[y,x,3]>=32:return record[0]
        return None

    def _make_room(self):
        c=self.layout['scene_canvas'];image=np.zeros((c[3]-c[1],c[2]-c[0],4),np.uint8)
        def local(points):return np.rint([(p[0]-c[0],p[1]-c[1]) for p in points]).astype(np.int32)
        def line(a,b,color,thickness=1):cv2.line(image,tuple(local([a])[0]),tuple(local([b])[0]),color,max(1,int(thickness*self.s)),cv2.LINE_AA)
        back=[self.plane_to_screen(x,y,'background') for x,y in ((0,0),(1,0),(1,1),(0,1))]
        ground=[self.plane_to_screen(x,y,'ground') for x,y in ((0,0),(1,0),(1,1),(0,1))]
        cv2.fillConvexPoly(image,local(back),(224,232,234,118),cv2.LINE_AA)
        for col in range(9):line(self.plane_to_screen(col/8,0,'background'),self.plane_to_screen(col/8,1,'background'),(79,95,102,92))
        for row in range(5):line(self.plane_to_screen(0,row/4,'background'),self.plane_to_screen(1,row/4,'background'),(79,95,102,92))
        cv2.fillConvexPoly(image,local(ground),(42,52,57,153),cv2.LINE_AA)
        for col in range(9):line(self.plane_to_screen(col/8,0),self.plane_to_screen(col/8,1),(214,225,228,160))
        for row in range(7):line(self.plane_to_screen(0,row/6),self.plane_to_screen(1,row/6),(214,225,228,160))
        cv2.polylines(image,[local(back)],True,(215,226,229,175),max(1,int(2*self.s)),cv2.LINE_AA)
        cv2.polylines(image,[local(ground)],True,(223,233,235,195),max(1,int(2*self.s)),cv2.LINE_AA)
        image[:,:,:3]=np.rint(image[:,:,:3]*image[:,:,3:4].astype(np.float32)/255.).astype(np.uint8);return image

    def _draw_scene(self,state):
        records=self._object_records(state);selected=getattr(state,'selected_id',None)
        if self._scene_initialized and records==self._scene_records and selected==self._scene_selected:return
        if not self._scene_initialized:dirty=(0,0,self._scene.shape[1],self._scene.shape[0]);self._counts['scene_rebuilds']+=1
        else:
            old={r[0]:r for r in self._scene_records};new={r[0]:r for r in records};affected={k for k in set(old)|set(new) if old.get(k)!=new.get(k)}
            if selected!=self._scene_selected:affected.update((selected,self._scene_selected))
            dirty=None
            for identifier in affected:
                for mapping in (old,new):
                    if identifier in mapping:r=self._object_rect(mapping[identifier],True);dirty=_union(dirty,(r[0]-3,r[1]-3,r[2]+3,r[3]+3))
            dirty=_intersect(dirty,(0,0,self._scene.shape[1],self._scene.shape[0])) if dirty else None
            self._counts['scene_dirty_updates']+=1
        if dirty:
            x1,y1,x2,y2=dirty;self._scene[y1:y2,x1:x2]=self._scene_background[y1:y2,x1:x2]
            for record in records:
                rect=self._object_rect(record,True)
                if _intersect(rect,dirty) is None:continue
                sprite=self._sprite(record[1],record[4]);self._over_premult(self._scene,sprite,rect[0],rect[1],dirty)
                if record[0]==selected:
                    edge=np.empty_like(sprite);edge[:,:,:3]=(247,250,250);edge[:,:,3]=cv2.subtract(cv2.dilate(sprite[:,:,3],np.ones((3,3),np.uint8)),cv2.erode(sprite[:,:,3],np.ones((3,3),np.uint8)))
                    self._over_premult(self._scene,edge,rect[0],rect[1],dirty)
            self._scene_inverse[y1:y2,x1:x2]=255-self._scene[y1:y2,x1:x2,3:4];self._counts['scene_dirty_pixels']+=(x2-x1)*(y2-y1)
        self._scene_records,self._scene_selected=records,selected;self._scene_initialized=True;self._draw_order=[r[0] for r in records]

    def _text(self,image,label,x,y,size=20,color=(54,65,70),align='left'):
        return self.ui.text(image,label,self._p(x,y),max(10,int(size*self.s)),color,align)

    def _button(self,image,name,icon=None):
        rect=self.layout[name]
        if icon in ('close','undo','check','plus','minus','back'):self.ui.button(image,rect,icon);return
        self.ui.panel(image,rect,(76,88,93),.49,radius=int(15*self.s),shadow=False)
        x1,y1,x2,y2=rect;cx,cy=(x1+x2)//2,(y1+y2)//2;r=max(6,int(min(x2-x1,y2-y1)*.21));color=(240,246,247);thick=max(1,int(2*self.s))
        if name in ('clear','delete'):
            cv2.rectangle(image,(cx-r,cy-r+3),(cx+r,cy+r),color,thick,cv2.LINE_AA);cv2.line(image,(cx-r-3,cy-r),(cx+r+3,cy-r),color,thick,cv2.LINE_AA);cv2.line(image,(cx-r//2,cy-r-4),(cx+r//2,cy-r-4),color,thick,cv2.LINE_AA)
        elif name=='copy':
            cv2.rectangle(image,(cx-r-3,cy-r-3),(cx+r-3,cy+r-3),color,thick,cv2.LINE_AA);cv2.rectangle(image,(cx-r+3,cy-r+3),(cx+r+3,cy+r+3),color,thick,cv2.LINE_AA)

    def _pattern(self,image,rect,mask):
        l,t,r,b=rect;cw,ch=(r-l)/7.,(b-t)/7.
        for y in range(7):
            for x in range(7):
                x1,y1=int(round(l+x*cw)),int(round(t+(6-y)*ch));x2,y2=int(round(l+(x+1)*cw)),int(round(t+(7-y)*ch));inset=max(1,int(min(cw,ch)*.06))
                if mask[y,x]:
                    cv2.rectangle(image,(x1+inset,y1+inset),(x2-inset,y2-inset),(157,170,176),-1)
                    cv2.rectangle(image,(x1+inset,y1+inset),(x2-inset,y2-inset),(248,251,252),max(1,int(2*self.s)),cv2.LINE_AA)
                    cv2.line(image,(x1+inset+2,y2-inset-2),(x2-inset-2,y2-inset-2),(102,119,127),max(1,int(4*self.s)),cv2.LINE_AA)
                else:cv2.rectangle(image,(x1+inset,y1+inset),(x2-inset,y2-inset),(108,131,142),1,cv2.LINE_AA)

    def _live_category(self,state):return getattr(state.model,'live_category',None) or getattr(state.model,'matched_category',None)

    def _static_draw(self,image,state):
        self.ui.panel(image,self.layout['panel'],(212,224,230),.27,radius=int(28*self.s))
        if state.stage=='edit':
            br=self.layout['board'];self.ui.panel(image,(br[0]-12,br[1]-12,br[2]+12,br[3]+12),(56,75,86),.22,radius=int(14*self.s),shadow=False);self._pattern(image,br,state.model.board)
            self.ui.panel(image,self.layout['preview'],(231,239,240),.17,radius=int(25*self.s),shadow=False);category=self._live_category(state)
            if category in TITLES:
                sprite=self._sprite(category,role='preview');p=self.layout['preview'];self._alpha(image,sprite,int((p[0]+p[2]-sprite.shape[1])*.5),int((p[1]+p[3]-sprite.shape[0])*.5))
            else:
                p=self.layout['preview'];cx,cy=(p[0]+p[2])//2,(p[1]+p[3])//2;cv2.rectangle(image,(cx-int(34*self.s),cy-int(34*self.s)),(cx+int(34*self.s),cy+int(34*self.s)),(156,174,185),max(1,int(2*self.s)),cv2.LINE_AA)
            for name,icon in (('clear',None),('back','undo'),('submit','check')):self._button(image,name,icon)
        else:
            for name,rect in self._library_rects(state).items():
                bp=next(b for b in state.model.library if 'library_'+str(b.id)==name);self.ui.panel(image,rect,(215,229,234),.25,radius=int(12*self.s),shadow=False);sprite=self._sprite(bp.category,role='thumb')
                self._alpha(image,sprite,rect[0]+int(10*self.s),int((rect[1]+rect[3]-sprite.shape[0])*.5));self.ui.text(image,TITLES[bp.category],(rect[0]+int(75*self.s),rect[1]+int(14*self.s)),max(12,int(17*self.s)),(53,68,76))
            self._button(image,'new','plus')
            for name in ('copy','delete'):self._button(image,name)

    def _build_layer(self,state,key):
        x1,y1,x2,y2=self._ui_rect;shape=(y2-y1,x2-x1,3)
        if self._ui_layer is None or self._ui_layer[0].shape!=shape:
            black=np.empty(shape,np.uint8);inverse=np.empty(shape,np.uint8)
        else:black,inverse=self._ui_layer
        white=self._blend_work if self._blend_work is not None and self._blend_work.shape==shape else np.empty(shape,np.uint8)
        black.fill(0);white.fill(255)
        # The existing drawing code is translated to the panel-local canvas.
        # Its layout dictionary is replaced temporarily, never mutated in place,
        # so app.layout and published immutable packets retain their coordinates.
        original_layout=self.layout;original_origin=self.ox,self.oy
        try:
            self.ox-=x1;self.oy-=y1
            self.layout={name:(r[0]-x1,r[1]-y1,r[2]-x1,r[3]-y1) for name,r in original_layout.items()}
            self._static_draw(black,state);self._static_draw(white,state)
        finally:
            self.layout=original_layout;self.ox,self.oy=original_origin
        cv2.subtract(white,black,dst=inverse)
        self._ui_layer=black,inverse;self._blend_work=white;self._ui_key=key;self._counts['ui_rebuilds']+=1
        self._counts['ui_build_region_pixels']=shape[0]*shape[1]
        self._counts['ui_build_full_frame_buffers']=0

    def visual_key(self,state,now):
        """Camera-free immutable state identity; no classification or ticking."""
        library=tuple((b.id,b.category,b.variant,getattr(b,'shape_variant',None)) for b in state.model.library)
        feedback=str(getattr(state,'feedback','')) if float(now)<float(getattr(state,'feedback_until',0)) else ''
        candidate=getattr(state,'candidate_cell',None) if state.stage=='edit' else None
        return (state.stage,state.model.board.tobytes() if state.stage=='edit' else None,library,
            self._live_category(state),self._object_records(state) if state.stage=='scene' else (),
            getattr(state,'selected_id',None) if state.stage=='scene' else None,
            tuple(candidate) if candidate is not None else None,feedback)

    def prepare(self,state,now,revision):
        """Snapshot two nonoverlapping local coefficient patches, never video.

        The original panel and room caches are combined on the UI owner thread.
        Black/white local endpoints preserve old blend rounding; the display
        loop receives only immutable byte coefficients and performs one blend.
        """
        from .layers import PreparedUI,OverlayPatch
        started=time.perf_counter();key=self.visual_key(state,now)
        if key==getattr(self,'_prepared_key',None):
            self._counts['prepare_cache_hits']=self._counts.get('prepare_cache_hits',0)+1
            return self._prepared
        if state.stage not in ('scene','edit'):raise ValueError('Grid studio has only scene and edit stages')
        self._sync_layout(state)
        library=key[2]
        ui_key=state.stage,state.model.board.tobytes() if state.stage=='edit' else None,library,self._live_category(state)
        if ui_key!=self._ui_key:self._build_layer(state,ui_key)
        premult,inverse=self._ui_layer
        black=premult.copy();white=cv2.add(premult,inverse)
        px,py,_,_=self._ui_rect
        if state.stage=='scene':
            self._draw_scene(state)
            c=self.layout['scene_canvas'];local=(c[0]-px,c[1]-py,c[2]-px,c[3]-py)
            for target in (black,white):
                roi=target[local[1]:local[3],local[0]:local[2]]
                cv2.multiply(roi,self._scene_inverse,dst=self._scene_work,scale=1/255.,dtype=cv2.CV_8U)
                cv2.add(self._scene_work,self._scene[:,:,:3],dst=roi)
        elif key[6] is not None:
            x,y=map(int,key[6])
            if 0<=x<7 and 0<=y<7:
                cx,cy=self.cell_center(x,y);r=self.layout['board'];cw,ch=(r[2]-r[0])/7.,(r[3]-r[1])/7.
                a=int(cx-cw*.47)-px,int(cy-ch*.47)-py;b=int(cx+cw*.47)-px,int(cy+ch*.47)-py
                for target in (black,white):cv2.rectangle(target,a,b,(241,248,250),max(2,int(2*self.s)),cv2.LINE_AA)
        if key[7]:
            point=self._p(816,110);point=point[0]-px,point[1]-py
            for target in (black,white):self.ui.text(target,key[7],point,max(10,int(17*self.s)),(57,73,83),'center')
        inverse=cv2.subtract(white,black)
        panel=OverlayPatch(self._ui_rect,black,inverse)
        if not hasattr(self,'_close_patch'):
            r=self.layout['close'];shape=(r[3]-r[1],r[2]-r[0],3)
            cb=np.zeros(shape,np.uint8);cw=np.full(shape,255,np.uint8)
            for target in (cb,cw):self.ui.button(target,(0,0,shape[1],shape[0]),'close')
            self._close_patch=OverlayPatch(r,cb,cv2.subtract(cw,cb))
        region=(self._ui_rect[0],self._ui_rect[1],self._ui_rect[2],min(self._ui_rect[3],self._ui_rect[1]+int(72*self.s)))
        self._prepared=PreparedUI(int(revision),float(now),self.width,self.height,(panel,self._close_patch),(region,))
        self._prepared_key=key;self._counts['prepare_count']=self._counts.get('prepare_count',0)+1
        self._counts['last_prepare_ms']=(time.perf_counter()-started)*1000
        return self._prepared

    def render(self,state,bgr,now):
        if bgr.dtype!=np.uint8 or bgr.shape!=self._output.shape:raise ValueError('Expected a uint8 BGR frame with renderer dimensions')
        if state.stage not in ('edit','scene'):raise ValueError('Grid studio has only scene and edit stages')
        self._sync_layout(state)
        np.copyto(self._output,bgr);library=tuple((b.id,b.category,b.variant,getattr(b,'shape_variant',None)) for b in state.model.library)
        key=state.stage,state.model.board.tobytes() if state.stage=='edit' else None,library,self._live_category(state)
        if key!=self._ui_key:self._build_layer(state,key)
        x1,y1,x2,y2=self._ui_rect;roi=self._output[y1:y2,x1:x2];premult,inverse=self._ui_layer
        cv2.multiply(roi,inverse,dst=self._blend_work,scale=1/255.,dtype=cv2.CV_8U);cv2.add(self._blend_work,premult,dst=roi)
        if state.stage=='scene':
            self._draw_scene(state);c=self.layout['scene_canvas'];roi=self._output[c[1]:c[3],c[0]:c[2]]
            cv2.multiply(roi,self._scene_inverse,dst=self._scene_work,scale=1/255.,dtype=cv2.CV_8U);cv2.add(self._scene_work,self._scene[:,:,:3],dst=roi)
        else:
            candidate=getattr(state,'candidate_cell',None)
            if candidate is not None and len(candidate)==2:
                x,y=map(int,candidate)
                if 0<=x<7 and 0<=y<7:
                    cx,cy=self.cell_center(x,y);r=self.layout['board'];cw,ch=(r[2]-r[0])/7.,(r[3]-r[1])/7.
                    cv2.rectangle(self._output,(int(cx-cw*.47),int(cy-ch*.47)),(int(cx+cw*.47),int(cy+ch*.47)),(241,248,250),max(2,int(2*self.s)),cv2.LINE_AA)
        self._button(self._output,'close','close')
        if getattr(state,'feedback','') and float(now)<float(getattr(state,'feedback_until',0)):self._text(self._output,str(state.feedback),816,110,17,(57,73,83),'center')
        self._counts['draw_count']+=1;return self._output

    def metrics(self):
        result=dict(self._counts);arrays=[self._output,self._scene,self._scene_background,self._scene_inverse,self._scene_work];arrays.extend(self._sprites.values());arrays.extend(self._scaled.values())
        if self._ui_layer:arrays.extend(self._ui_layer);arrays.append(self._blend_work)
        packet=getattr(self,'_prepared',None)
        prepared_bytes=sum(p.premult.nbytes+p.inverse.nbytes for p in packet.patches) if packet else 0
        result.update(decoded_assets=len(self._sprites),scaled_cache_entries=len(self._scaled),scaled_cache_limit=24,buffer_bytes=sum(a.nbytes for a in arrays),prepared_packet_bytes=prepared_bytes,draw_order=list(self._draw_order),transparent_planes=True,grid_sizes={'ground':[8,6],'background':[8,4]},projection='fixed orthographic affine',camera_yaw_deg=self.camera_yaw_deg,camera_pitch_deg=self.camera_pitch_deg,scene_unit_pixels=self.scene_unit,depth_order='camera dot world; stable creation ties',tree_cached_display_aspect=self._sprites['tree'].shape[1]/self._sprites['tree'].shape[0]);return result
