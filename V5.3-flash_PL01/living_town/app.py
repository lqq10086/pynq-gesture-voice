"""Event-driven town creation using the stable, time-based pinch controller.

Exact shape matching is cached after an editor mutation. Display frames neither
reclassify shapes nor manufacture clicks. Camera mirroring remains the
responsibility of the existing input adapter.
"""
from __future__ import annotations

import math
from pathlib import Path
import time

from spatial_build.controls import GestureController


class TownApp:
    """A retained scene home and a 7 x 7 live-preview editor."""

    def __init__(self, width=1280, height=720, font_path=None, root=None,
                 work_path=None, model=None, renderer=None):
        from .model import TownModel
        from .render import TownRenderer

        self.width, self.height = int(width), int(height)
        if self.width <= 0 or self.height <= 0:
            raise ValueError("positive output dimensions are required")
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        self.model = model if model is not None else TownModel(root=self.root)
        self.renderer = renderer if renderer is not None else TownRenderer(
            width=self.width, height=self.height, root=self.root, font_path=font_path)
        self.layout = self.renderer.layout
        self.controls = GestureController()
        self.controller = self.controls
        self.work_path = Path(work_path) if work_path is not None else self.root / 'works' / 'latest.json'
        self.stage = 'scene'
        self.selected_id = None
        self.candidate_cell = None
        self.drag_preview = None
        self._drag_offset = None
        self.feedback = ''
        self.feedback_until = 0.0
        self.quit_requested = False
        self.debug = False
        self.detections = ()
        self.last_observation = None
        self.render_count = 0
        self.last_render_ms = 0.0
        self._fresh_count = 0
        self._stale_count = 0

    @property
    def metrics(self):
        return dict(fresh_observations=self._fresh_count,
                    ignored_stale=self._stale_count,
                    render_count=self.render_count,
                    last_render_ms=self.last_render_ms,
                    match_count=self.model.match_count,
                    submit_count=self.model.submit_count)

    @property
    def selected_id(self):
        return self.model.selected_id

    @selected_id.setter
    def selected_id(self, value):
        self.model.selected_id = value

    def _feedback(self, message, now):
        self.feedback = message
        self.feedback_until = float(now) + 2.4

    def _switch(self, stage):
        self.stage = stage
        self.candidate_cell = None
        self.drag_preview = None
        self._drag_offset = None
        # A held Submit / New pinch cannot fall through into the next page.
        self.controls.reset(require_release=True)

    def hit_test(self, point):
        """Adapt the renderer's pixel hit to the established controller API."""
        hit = self.renderer.hit_test(point, self)
        if not hit:
            return None
        if hit[0] == 'cell' and len(hit) == 3:
            return 'select', ('cell', int(hit[1]), int(hit[2]))
        if hit[0] == 'object' and len(hit) == 2:
            return 'drag', hit[1]
        if hit[0] == 'empty':
            return 'empty', None
        if len(hit) == 2 and hit[0] in ('button', 'select', 'drag'):
            return hit
        return None

    def observe(self, detections, now):
        """Consume a single fresh detection publication, never a display frame."""
        now = float(now)
        if not math.isfinite(now):
            raise ValueError('now must be finite')
        if self.last_observation is not None and now <= self.last_observation:
            self._stale_count += 1
            return []
        self.last_observation = now
        self._fresh_count += 1
        self.detections = tuple(detections)
        events = self.controls.observe(self.detections, self.width, self.height,
                                      now, self.hit_test, allow_zoom=False,
                                      allow_palm_rotate=False)
        self.handle_events(events, now)
        self._candidate()
        return events

    def _candidate(self):
        self.candidate_cell = None
        if self.stage == 'edit' and self.controls.pointer is not None:
            hit = self.hit_test(self.controls.pointer)
            if hit and hit[0] == 'select' and hit[1][0] == 'cell':
                self.candidate_cell = (hit[1][1], hit[1][2])

    def tick(self, now):
        now = float(now)
        events = self.controls.tick(now)
        self.handle_events(events, now)
        if self.feedback and now >= self.feedback_until:
            self.feedback = ''
        self._candidate()
        return events

    def render(self, bgr, now=None):
        now = time.perf_counter() if now is None else float(now)
        self.tick(now)
        started = time.perf_counter()
        output = self.renderer.render(self, bgr, now)
        self.last_render_ms = (time.perf_counter() - started) * 1000.0
        self.render_count += 1
        return output

    def _object(self, object_id):
        return next((obj for obj in self.model.instances if obj.id == object_id), None)

    def _start_drag(self, object_id, point):
        obj = self._object(object_id)
        if obj is None or point is None:
            return
        self.selected_id = object_id
        plane_point = self.renderer.screen_to_plane(point, obj.plane)
        if plane_point is None:
            return
        self.drag_preview = (object_id, obj.x, obj.y)
        self._drag_offset = (obj.x - plane_point[0], obj.y - plane_point[1])

    def _move_drag(self, point, recovered=False):
        if self.drag_preview is None or point is None:
            return
        object_id, x, y = self.drag_preview
        obj = self._object(object_id)
        if obj is None:
            self._finish_drag(commit=False)
            return
        plane_point = self.renderer.screen_to_plane(point, obj.plane)
        if plane_point is None:
            return
        if recovered:
            # A reacquired hand is a new reference position, not a teleport.
            self._drag_offset = (x - plane_point[0], y - plane_point[1])
            return
        from .model import snap_position
        x, y = snap_position(obj.category, plane_point[0] + self._drag_offset[0],
                             plane_point[1] + self._drag_offset[1])
        self.drag_preview = (object_id, x, y)

    def _finish_drag(self, commit):
        if commit and self.drag_preview is not None:
            object_id, x, y = self.drag_preview
            self.model.move(object_id, x, y)
        self.drag_preview = None
        self._drag_offset = None

    def cancel_interaction(self, now=None):
        """Safely stop a failed input stream without treating it as release.

        A fatal inference/driver failure is not evidence that a user released
        the pinch. Preserve both the original object and current editor, drop
        the uncommitted preview, and require a real release before rearming.
        """
        self._finish_drag(commit=False)
        self.controls.reset(require_release=True)
        self.candidate_cell = None
        self.detections = ()

    def complete(self, now):
        if self.stage != 'edit':
            return False
        category = self.model.submit()
        if category is None:
            self._feedback('调整造型', now)
            return False
        if len(self.model.instances) >= 12:
            self._feedback('最多12个物体', now)
            return False
        try:
            blueprint = self.model.make_blueprint()
            # New objects start at the near edge so an older tall sprite cannot
            # hide their entire grab area before the user has placed them.
            instance = self.model.add_instance(blueprint.id, y=.90, mask=self.model.board)
        except (ValueError, KeyError):
            self._feedback('调整造型', now)
            return False
        self.selected_id = instance.id
        self.feedback = ''
        self._switch('scene')
        return True

    def go_back(self):
        if self.stage == 'edit':
            self._switch('scene')
        else:
            self.quit_requested = True
        return True

    def _button(self, target, now):
        if target in ('close', 'back'):
            return self.go_back()
        if self.stage == 'edit':
            if target in ('clear', 'reset', 'redraw'):
                self.model.clear()
            elif target in ('submit', 'complete'):
                self.complete(now)
            elif target in ('view_scene', 'scene'):
                self._switch('scene')
            return
        if self.stage != 'scene':
            return
        if target in ('new', 'create'):
            self.model.clear()
            self.selected_id = None
            self._switch('edit')
        elif target == 'copy':
            if self.selected_id is not None:
                try:
                    result = self.model.copy_instance(self.selected_id)
                except (ValueError, KeyError):
                    self._feedback('最多12个物体', now)
                else:
                    self.model.move(result.id, result.x, .90)
                    self.selected_id = result.id
        elif target == 'delete':
            if self.selected_id is not None:
                self.model.delete_instance(self.selected_id)
                self.selected_id = None
        elif target.startswith('library_'):
            try:
                instance = self.model.add_instance(target[8:], y=.90)
            except (ValueError, KeyError):
                self._feedback('最多12个物体', now)
            else:
                self.selected_id = instance.id
    def handle_events(self, events, now):
        for event in events:
            kind = event['kind']
            intent, target = event.get('intent'), event.get('target')
            if kind == 'click':
                if intent == 'button' and isinstance(target, str):
                    self._button(target, now)
                elif intent == 'select' and self.stage == 'edit':
                    if isinstance(target, tuple) and len(target) == 3 and target[0] == 'cell':
                        self.model.toggle(target[1], target[2])
                        self.feedback = ''
                elif intent == 'empty' and self.stage == 'scene':
                    self.selected_id = None
            elif kind == 'drag_start' and self.stage == 'scene':
                self._start_drag(target, event.get('point'))
            elif kind == 'drag_move' and self.stage == 'scene':
                self._move_drag(event.get('point'), event.get('recovered', False))
            elif kind == 'drag_end':
                self._finish_drag(commit=self.stage == 'scene')
            elif kind == 'cancel':
                self._finish_drag(commit=False)

