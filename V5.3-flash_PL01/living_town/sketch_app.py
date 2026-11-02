"""Stroke editor over the retained town scene and time-based gesture controller."""
import math
from pathlib import Path
import time

from .app import TownApp
from .sketch_model import SketchTownModel, SKETCH_WIDTH, SKETCH_HEIGHT

class SketchTownApp(TownApp):
    def __init__(self, width=1280, height=720, font_path=None, root=None,
                 work_path=None, model=None, renderer=None):
        if renderer is None:
            from .sketch_render import SketchTownRenderer
            renderer = SketchTownRenderer(width,height,root=root,font_path=font_path)
        model = SketchTownModel(root=root) if model is None else model
        super().__init__(width,height,font_path=font_path,root=root,work_path=work_path,
                         model=model,renderer=renderer)
        self.drawing_interval_ms = 0
        self.brush_point = None
        self._last_brush_at = None
        self._pending_paint = []

    @property
    def requested_detection_interval_ms(self):
        # No artificial inference pacing on either page. The existing worker
        # consumes the newest clean frame only when its previous NN has finished.
        return 0

    @property
    def metrics(self):
        result = super().metrics
        result.update(mode='standalone_sketch_town',drawing_interval_ms=self.drawing_interval_ms,
            requested_detection_interval_ms=self.requested_detection_interval_ms,
            stroke_count=len(self.model.strokes),point_count=self.model.point_count,
            scene_objects=len(self.model.instances),library_entries=len(self.model.library),
            sketch_revision=self.model.sketch_revision,stroke_active=self.model.stroke_active,
            classifier_runs_only_on_submit=True)
        return result

    def _logical_point(self, point):
        if point is None:
            return None
        l,t,r,b = self.layout['board']
        if not (l <= point[0] < r and t <= point[1] < b):
            return None
        return ((float(point[0])-l)*SKETCH_WIDTH/(r-l),
                (float(point[1])-t)*SKETCH_HEIGHT/(b-t))

    def _switch(self, stage):
        if stage not in ('scene','edit'):
            raise ValueError('the sketch town has only scene and edit pages')
        self.model.end_stroke()
        self.brush_point = self._last_brush_at = None
        self._pending_paint = []
        super()._switch(stage)

    def _candidate(self):
        self.candidate_cell = None

    def observe(self, detections, now):
        now = float(now)
        if not math.isfinite(now):
            raise ValueError('now must be finite')
        if self.last_observation is not None and now <= self.last_observation:
            self._stale_count += 1
            return []
        self.last_observation = now
        self._fresh_count += 1
        self.detections = tuple(detections)
        points = self.controls._points(self.detections,'pinch_point')
        point = (min(points,key=lambda p:math.dist(p[1],self.controls._last_point))[1]
                 if points and self.controls._last_point is not None else points[0][1] if points else None)
        logical = self._logical_point(point) if self.stage == 'edit' else None
        if logical is not None and self.controls.phase in ('idle','pending'):
            if self.controls.phase == 'idle' or self.controls.target != 'paint':
                self._pending_paint = []
            self._pending_paint.append((now,logical))
            self._pending_paint = [(stamp,p) for stamp,p in self._pending_paint[-4:] if now-stamp <= .85]
        elif not points:
            self._pending_paint = []
        events = self.controls.observe(self.detections,self.width,self.height,now,
                                      self.hit_test,allow_zoom=False,allow_palm_rotate=False)
        if (logical is not None and self.controls.phase in ('pending','active')
                and self.controls.target == 'paint'):
            self.brush_point, self._last_brush_at = tuple(point), now
        else:
            self.brush_point = None
        self.handle_events(events,now)
        return events

    def tick(self, now):
        events = super().tick(now)
        # Timeout updates may hide a cursor, but can never append a sample.
        if self._last_brush_at is None or now-self._last_brush_at > .5:
            self.brush_point = None
        return events

    def _begin_paint(self, point, now):
        logical = self._logical_point(point)
        if logical is None:
            return
        buffered = [p for stamp,p in self._pending_paint if now-stamp <= .85]
        first = buffered[0] if buffered else logical
        if not self.model.begin_stroke(first):
            self._feedback('笔迹已满',now)
            self.controls.reset(require_release=True)
            self.brush_point = None
            return
        for sample in buffered[1:]:
            self.model.append_point(sample)
        self.model.append_point(logical)
        self.feedback = ''
        self._pending_paint = []

    def handle_events(self, events, now):
        for event in events:
            kind,target = event.get('kind'),event.get('target')
            if self.stage == 'edit' and target == 'paint' and kind in ('drag_start','drag_move','drag_end'):
                if kind == 'drag_start':
                    self._begin_paint(event.get('point'),now)
                elif kind == 'drag_move':
                    point = self._logical_point(event.get('point'))
                    if point is None:
                        self.model.end_stroke()
                        self.brush_point = None
                    elif event.get('recovered') or not self.model.stroke_active:
                        # A missed/released pen is a new segment: no accidental
                        # long chord through a house, wheel or toolbar.
                        self.model.begin_stroke(point)
                    else:
                        self.model.append_point(point)
                else:
                    self.model.end_stroke()
                    self.brush_point = None
                continue
            if kind == 'cancel':
                self.model.end_stroke()
                self.brush_point = None
                self._pending_paint = []
            super().handle_events((event,),now)

    def cancel_interaction(self, now=None):
        self.model.end_stroke()
        self.brush_point = self._last_brush_at = None
        self._pending_paint = []
        super().cancel_interaction(now)

    def complete(self, now):
        if self.stage != 'edit':
            return False
        self.model.end_stroke()
        category = self.model.submit()
        if category is None:
            self._feedback('请补充轮廓',now)
            return False
        if len(self.model.instances) >= 12:
            self._feedback('最多12个物体',now)
            return False
        try:
            blueprint = self.model.make_blueprint()
            instance = self.model.add_instance(blueprint.id,y=.90,mask=self.model._source_mask(category),
                                               strokes=self.model.strokes,
                                               source_kind=self.model._source_kind)
        except (ValueError,KeyError):
            self._feedback('请补充轮廓',now)
            return False
        self.selected_id = instance.id
        self.feedback = ''
        self._switch('scene')
        return True

    def _button(self, target, now):
        if self.stage == 'edit' and target in ('clear','reset','redraw','submit','complete'):
            self.cancel_interaction(now)
        return super()._button(target,now)

    def apply_voice_command(self, label, now=None):
        # Town never assigned ON/OFF; other games' number words stay inactive.
        return self.go_back() if label == 'back' else False
