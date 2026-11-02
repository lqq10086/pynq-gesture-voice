"""Opaque controls and sparse motion geometry, no glass or large panel."""
import time
import cv2
import numpy as np

from spatial_build.model import LEVELS
from town_creator.layers import PreparedUI
from .observe_flat_ui import opaque_packet, ui_regions
from .spatial_layers import spatial_key


class CombinedRenderer:
    def __init__(self, app):
        self.owner = app
        self._key = self._packet = self._canvas = None
        self.preparations = self.cache_hits = self.draw_calls = 0
        self.last_prepare_ms = 0.0
        self.prepared_scene = self._snapshot_source = None
        self._controls_canvas = self._controls_mask = self._controls_key = None
        self._controls_template = None

    def visual_key(self, app, now):
        prefix = (app.mode, int(app.mode_revision))
        if app.stage == 'levels':
            pointer = tuple(app.controls.pointer) if app.controls.pointer is not None else None
            return prefix + ('levels', int(app.selected_entry), tuple(app.level_rects.items()), pointer, bool(app.debug))
        motion=bool(getattr(app.spatial,'continuous_rotating',False))
        pixel_scale=2 if motion and not app.debug and app.spatial.renderer._native is not None else 1
        return prefix + ('spatial', spatial_key(app.spatial, now),motion,pixel_scale)

    def _draw_levels(self, image, app):
        self.prepared_scene = self._snapshot_source = None
        ui = app.spatial.ui
        ui.button(image, app.layout['close'], 'close')
        for i, level in enumerate(LEVELS):
            rect = app.level_rects['level_' + level.id]
            ui.panel(image, rect, color=(105,105,105) if app.selected_entry == i+1 else (75,75,75))
            cx = (rect[0]+rect[2])//2
            ui.text(image, str(i+1), (cx, rect[1]+int(.035*app.height)),
                    size=int(.075*app.height), align='center')
            ui.text(image, level.name, (cx, rect[1]+int(.16*app.height)),
                    size=int(.030*app.height), align='center')
        app.spatial.visible_controls = ('close',) + tuple(app.level_rects)

    def _draw_observe(self, image, app):
        spatial = app.spatial
        spatial.scene = spatial.renderer.render(image, spatial.board.blocks, spatial.layout['observe'],
            yaw_deg=spatial.yaw, pitch_deg=spatial.pitch, scale=spatial.scale,
            selected_id=spatial.selected_id, grid=False,
            motion=bool(getattr(spatial, 'continuous_rotating', False)),
            unit_px=spatial.observe_unit_px)
        if self._snapshot_source is not spatial.scene:
            self.prepared_scene = spatial.scene.snapshot()
            self._snapshot_source = spatial.scene
        self._draw_observe_controls(image,app)

    @staticmethod
    def _draw_observe_controls(image,app):
        spatial=app.spatial
        ui = spatial.ui
        ui.button(image, app.layout['close'], 'close')
        ui.button(image, spatial.layout['view_reset'], 'reset')
        ui.button(image, spatial.layout['replay'], 'replay')
        ui.palm_hint(image, spatial.layout['palm_hint'], active=app.controls.active_intent == 'rotate')
        spatial.visible_controls = ('close', 'view_reset', 'replay')

    def _draw(self, image, app):
        if app.stage == 'levels': self._draw_levels(image, app)
        else: self._draw_observe(image, app)
        if app.controls.pointer is not None:
            cv2.circle(image, tuple(int(v) for v in app.controls.pointer),
                       max(3, int(app.height*.006)), (204,210,212), 1, cv2.LINE_AA)
        if app.debug:
            for detection in app.spatial.detections:
                x1,y1,x2,y2 = detection.box
                cv2.rectangle(image,(x1,y1),(x2,y2),(160,160,160),1)
                cv2.putText(image, '%s %.2f' % (detection.label,detection.confidence),
                            (x1,max(15,y1-4)),cv2.FONT_HERSHEY_SIMPLEX,.45,(230,230,230),1,cv2.LINE_AA)
        self.draw_calls += 1

    @staticmethod
    def _revision(packet, revision, now):
        if packet.revision == int(revision): return packet
        if getattr(packet,'native_opaque',False): return packet.with_revision(revision,now)
        return PreparedUI(int(revision),float(now),packet.width,packet.height,packet.patches,())

    def _prepare_motion_native(self,app,now,revision):
        from town_creator.native_ui import compose_geometry,prepare_controls
        spatial=app.spatial;viewport=spatial.layout['observe']
        scene,bgr,mask=spatial.renderer.render_motion_native(spatial.board.blocks,viewport,
            (app.height,app.width,3),yaw_deg=spatial.yaw,pitch_deg=spatial.pitch,scale=spatial.scale,
            selected_id=spatial.selected_id,unit_px=spatial.observe_unit_px,pixel_scale=2)
        spatial.scene=scene;self.prepared_scene=self._snapshot_source=scene
        controls_key=(app.width,app.height,id(spatial.ui),app.mode_revision,app.layout['close'],
            tuple(spatial.layout[name] for name in ('view_reset','replay','palm_hint')),
            app.controls.active_intent=='rotate')
        if controls_key!=self._controls_key:
            self._controls_canvas=np.zeros((app.height,app.width,3),np.uint8)
            self._draw_observe_controls(self._controls_canvas,app)
            self._controls_mask=cv2.inRange(self._controls_canvas,(1,1,1),(255,255,255))
            self._controls_canvas=cv2.bitwise_and(self._controls_canvas,self._controls_canvas,mask=self._controls_mask)
            self._controls_canvas=np.frombuffer(self._controls_canvas.tobytes(),np.uint8).reshape(app.height,app.width,3)
            self._controls_mask=np.frombuffer(self._controls_mask.tobytes(),np.uint8).reshape(app.height,app.width)
            self._controls_template=prepare_controls(self._controls_canvas,self._controls_mask)
            self._controls_key=controls_key
        pointer=pointer_rect=None
        if app.controls.pointer is not None:
            x,y=(int(v) for v in app.controls.pointer);radius=max(3,int(app.height*.006))
            x0,y0=max(0,x-radius-2),max(0,y-radius-2)
            x1,y1=min(app.width,x+radius+3),min(app.height,y+radius+3)
            if x0<x1 and y0<y1:
                pointer=np.zeros((y1-y0,x1-x0,3),np.uint8)
                vx,vy,vx2,vy2=viewport
                a,b,c,d=max(x0,vx),max(y0,vy),min(x1,vx2),min(y1,vy2)
                if a<c and b<d:
                    yy=(np.arange(b,d)-vy)//2;xx=(np.arange(a,c)-vx)//2
                    pointer[b-y0:d-y0,a-x0:c-x0]=bgr[yy[:,None],xx[None,:]]
                controls=self._controls_canvas[y0:y1,x0:x1]
                foreground=self._controls_mask[y0:y1,x0:x1]
                cv2.copyTo(controls,foreground,pointer)
                # Draw over the actual tiny local background so AA rounding is
                # identical to the original full-canvas pointer, even on faces.
                cv2.circle(pointer,(x-x0,y-y0),radius,(204,210,212),1,cv2.LINE_AA)
                pointer_rect=(x0,y0,x1,y1)
        packet=compose_geometry(bgr,mask,viewport,self._controls_template,revision,now,
            pointer,pointer_rect,geometry_pose=(spatial.yaw,spatial.pitch,spatial.scale),preset_id=spatial.level.id,
            pixel_scale=2)
        spatial.visible_controls=('close','view_reset','replay')
        self.draw_calls+=1
        return packet

    def prepare(self, app, now, revision):
        started = time.perf_counter()
        key = self.visual_key(app, now)
        if key == self._key:
            self.cache_hits += 1
            if app.stage == 'observe': app._skip_post_prepare_animation_once = True
            return self._revision(self._packet, revision, now)
        native_motion=(app.stage=='observe' and getattr(app.spatial,'continuous_rotating',False)
            and not app.debug and app.spatial.renderer._native is not None)
        if native_motion:
            packet=self._prepare_motion_native(app,now,revision)
        else:
            if self._canvas is None:
                self._canvas = np.zeros((app.height,app.width,3),np.uint8)
            else: self._canvas.fill(0)
            self._draw(self._canvas, app)
            packet = opaque_packet(self._canvas, revision, now, regions=ui_regions(app))
        self._packet, self._key = packet, key
        self.preparations += 1
        if app.stage == 'observe': app._skip_post_prepare_animation_once = True
        self.last_prepare_ms = (time.perf_counter()-started)*1000.0
        return packet

    def release_inactive_scratch(self):
        self._canvas = None
        self._controls_canvas=self._controls_mask=self._controls_key=None
        self._controls_template=None

    def metrics(self):
        app = self.owner.spatial
        return dict(preparations=self.preparations,prepare_cache_hits=self.cache_hits,
            last_prepare_ms=self.last_prepare_ms,offscreen_draw_calls=self.draw_calls,
            draws_per_preparation=1,mode='observe_only',
            scratch_bytes=sum(a.nbytes for a in (self._canvas,self._controls_canvas,self._controls_mask) if a is not None),
            geometry_rasters=app.renderer.raster_count,projection_cache_builds=app.projection_stage.cache_build_count,
            geometry_backend=app.renderer.metrics(),
            camera_free=True,glass=False,board=False,display_compositor='opaque mask copyTo')
