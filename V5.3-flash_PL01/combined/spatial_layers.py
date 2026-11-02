"""Camera-free preparation around the unchanged original SpatialApp renderer.

Only this UI-owner adapter draws black/white endpoints. The video compositor
receives immutable byte coefficients, never either app's camera background.
"""
import time
import numpy as np
import cv2

from town_creator.layers import OverlayPatch,PreparedUI


def spatial_key(app,now):
    pointer=tuple(app.controls.pointer) if app.controls.pointer is not None else None
    feedback=app.feedback if app.stage=='build' and now<app.feedback_until else ''
    detections=tuple((d.label,float(d.confidence),tuple(d.box)) for d in app.detections) if app.debug else ()
    return (app.stage,getattr(app.level,'id',None),
        tuple((b.id,b.x,b.y,b.z) for b in app.board.blocks),app.carry,app.preview,app.check_result,
        (float(app.yaw),float(app.pitch),float(app.scale),app.selected_id,app.controls.active_intent) if app.stage=='observe' else None,
        pointer,feedback,bool(app.debug),detections)


def drawing_bounds(app):
    width,height=app.width,app.height
    if app.debug:return 0,0,width,height
    panel=app.layout['panel'];close=app.layout['close']
    x1,y1=min(panel[0]-5,close[0]),min(panel[1],close[1])
    x2,y2=max(panel[2]+5,close[2]),max(panel[3]+11,close[3])
    if app.controls.pointer is not None:
        px,py=map(float,app.controls.pointer);radius=max(3,int(height*.006))+2
        x1,y1=min(x1,int(px)-radius),min(y1,int(py)-radius)
        x2,y2=max(x2,int(px)+radius+1),max(y2,int(py)+radius+1)
    return max(0,x1),max(0,y1),min(width,x2),min(height,y2)


def packet_from_endpoints(black,white,rect,revision,now,glass):
    x1,y1,x2,y2=rect
    a=black[y1:y2,x1:x2]
    inverse=cv2.subtract(white[y1:y2,x1:x2],a)
    return PreparedUI(int(revision),float(now),black.shape[1],black.shape[0],
        (OverlayPatch(rect,a,inverse),),tuple(glass))


class SpatialLayers:
    def __init__(self,app):
        self.app=app;self._black=None;self._white=None
        self.preparations=self.offscreen_draw_calls=0;self.last_prepare_ms=0.

    def prepare(self,now,revision):
        app=self.app;started=time.perf_counter();shape=(app.height,app.width,3)
        if self._black is None:
            self._black=np.zeros(shape,np.uint8);self._white=np.full(shape,255,np.uint8)
        else:self._black.fill(0);self._white.fill(255)
        # Rendering remains the original code path, including projection assets,
        # legal-placement preview, selected-face occlusion and exact ID buffers.
        # Advancing simulation twice here would invalidate its prepared key;
        # the sole UI owner has already called the original tick once.
        sentinel=object();saved_tick=app.__dict__.get('tick',sentinel)
        count,last_ms=app.render_count,app.last_render_ms
        app.tick=lambda timestamp: None
        try:
            black=app.render(self._black,now)
            white=app.render(self._white,now)
        finally:
            if saved_tick is sentinel:del app.__dict__['tick']
            else:app.tick=saved_tick
            app.render_count,app.last_render_ms=count,last_ms
        panel=app.layout['panel'];glass=((panel[0],panel[1],panel[2],min(panel[3],panel[1]+72)),)
        packet=packet_from_endpoints(black,white,drawing_bounds(app),revision,now,glass)
        self.preparations+=1;self.offscreen_draw_calls+=2
        self.last_prepare_ms=(time.perf_counter()-started)*1000
        return packet

    def release_scratch(self):
        """Release offscreen preparation buffers; retain model and geometry IDs."""
        self._black=self._white=None

    def metrics(self):
        app=self.app
        return dict(preparations=self.preparations,offscreen_draw_calls=self.offscreen_draw_calls,
            last_prepare_ms=self.last_prepare_ms,scratch_bytes=sum(a.nbytes for a in (self._black,self._white) if a is not None),
            geometry_rasters=app.renderer.raster_count,projection_cache_builds=app.projection_stage.cache_build_count,
            camera_free=True,geometry_renderer_preserved=True)
