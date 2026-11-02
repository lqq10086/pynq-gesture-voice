"""One experience: level selection, legal construction, then inspection.

Camera pixels are already mirrored by the boundary adapter. No mirroring or
gesture inference happens here. All state changes use fresh observations.
"""
import math
import time

import cv2
import numpy as np

from .controls import GestureController
from .model import Board, LEVELS, LEVEL_BY_ID, WIDTH, DEPTH, MAX_HEIGHT, CAPACITY
from .renderer import Renderer, Scene, camera_basis
from .projection_stage import ProjectionStage, STAGE_BOUNDS, GAP, FLOOR_Z
from .ui import Ui


def inside(rect, point):
    return point is not None and rect[0] <= point[0] < rect[2] and rect[1] <= point[1] < rect[3]


class SpatialApp:
    INITIAL_YAW = 35.0
    INITIAL_PITCH = 28.0
    MIN_SCALE, MAX_SCALE = .65, 1.6

    def __init__(self, width=1280, height=720, font_path=None, voice_direct_entry=False):
        self.width, self.height = width, height
        self.voice_direct_entry = bool(voice_direct_entry)
        self.ui = Ui(width, height, font_path=font_path)
        self.renderer = Renderer()
        self.controls = GestureController()
        self.stage, self.level = 'levels', None
        self.selected_level_id = LEVELS[0].id
        self.board = Board()
        self.scene = None
        self.selected_id = None
        self.carry, self.preview = None, None
        self.check_result = None
        self.feedback, self.feedback_until = '', 0.0
        self.quit_requested = False
        self.debug = False
        self.detections = ()
        self.last_tick = None
        self.yaw = self.target_yaw = self.INITIAL_YAW
        self.pitch = self.target_pitch = self.INITIAL_PITCH
        self.scale = self.target_scale = 1.0
        self.layout = self._layout()
        self.projection_stage = ProjectionStage()
        self.build_projection = self._build_geometry()
        self.visible_controls = ()
        self.render_count = 0
        self.last_render_ms = 0.0

    def _rect(self, x1, y1, x2, y2):
        return tuple(int(v * (self.width if i % 2 == 0 else self.height))
                     for i, v in enumerate((x1, y1, x2, y2)))

    def _layout(self):
        return dict(panel=self._rect(.055, .15, .945, .94),
                    close=self._rect(.474, .055, .526, .138),
                    build=self._rect(.15, .22, .90, .80),
                    observe=self._rect(.16, .22, .84, .78),
                    palette=self._rect(.315, .28, .382, .39),
                    undo=self._rect(.64, .81, .71, .905),
                    reset=self._rect(.735, .81, .805, .905),
                    check=self._rect(.83, .81, .90, .905),
                    view_reset=self._rect(.72, .815, .79, .91),
                    replay=self._rect(.81, .815, .88, .91),
                    palm_hint=self._rect(.34, .82, .65, .905),
                    **{'level_' + lev.id: self._rect(.095 + i*.28, .36, .345 + i*.28, .73)
                       for i, lev in enumerate(LEVELS)})

    def _build_geometry(self):
        """One fixed transform shared by hint planes, blocks and hit testing."""
        lower, upper = (np.asarray(p, dtype=float) for p in STAGE_BOUNDS)
        pivot = (lower+upper)*.5
        right, up, camera = camera_basis(self.INITIAL_YAW, self.INITIAL_PITCH)
        corners = np.asarray([(x,y,z) for x in (lower[0],upper[0])
                             for y in (lower[1],upper[1]) for z in (lower[2],upper[2])])
        rect = self.layout['build']
        unit = min((rect[2]-rect[0])*.90 / np.ptp(corners@right),
                   (rect[3]-rect[1])*.92 / np.ptp(corners@up))
        return Scene(rect,pivot,unit,right,up,camera,(),None,None,None,[],(WIDTH,DEPTH))

    def _switch(self, stage):
        self.stage = stage
        self.carry = self.preview = None
        self.scene = None
        self.controls.reset(require_release=True)

    def choose_level(self, level_id):
        self.level = LEVEL_BY_ID[level_id]
        self.selected_level_id = level_id
        self.board = Board()
        self.selected_id = None
        self.check_result = None
        self.feedback = ''
        self.reset_view()
        self._switch('build')

    def go_back(self):
        """Return one level without discarding the construction in inspection."""
        if self.stage == 'observe':
            self.selected_id = None
            self._switch('build')
            return True
        if self.stage == 'build':
            self._switch('levels')
            return True
        return False

    def apply_voice_command(self, label, now=None):
        """Called only on the UI thread, after the KWS worker accepts a word."""
        now = time.perf_counter() if now is None else now
        choices = dict(zip(('one','two','three'), (level.id for level in LEVELS)))
        if label in choices and self.stage == 'levels':
            self.selected_level_id = choices[label]
            if self.voice_direct_entry:
                self.choose_level(self.selected_level_id)
                return True
            self.controls.reset(require_release=True)
            return True
        if label == 'on':
            if self.stage == 'levels':
                self.choose_level(self.selected_level_id)
                return True
            if self.stage == 'build':
                # Explicit submission never commits an unfinished drag or
                # bypasses the three-projection legality check.
                self.carry = self.preview = None
                self.controls.reset(require_release=True)
                return bool(self.check(now).passed)
        if label in ('off','back'):
            return self.go_back()
        return False

    def reset_view(self):
        # Keep angles unwrapped during interaction. Reset to the nearest
        # equivalent initial view, instead of unwinding every completed turn.
        self.target_yaw = self.INITIAL_YAW + 360.0 * round((self.yaw-self.INITIAL_YAW)/360.0)
        self.target_pitch = self.INITIAL_PITCH + 360.0 * round((self.pitch-self.INITIAL_PITCH)/360.0)
        self.target_scale = 1.0

    def check(self, now=None):
        if self.stage != 'build':
            return None
        self.check_result = self.board.judge(self.level)
        if self.check_result.passed:
            # Board, coordinates and stable IDs are preserved verbatim.
            self.yaw, self.pitch, self.scale = self.INITIAL_YAW, self.INITIAL_PITCH, 1.0
            self.reset_view()
            self.selected_id = None
            self._switch('observe')
        else:
            self._feedback('检查投影', time.perf_counter() if now is None else now)
        return self.check_result

    def _feedback(self, text, now):
        self.feedback, self.feedback_until = text, now + 2.0

    def _buttons(self):
        buttons = {'close': self.layout['close']}
        if self.stage == 'levels':
            buttons.update({key: val for key, val in self.layout.items() if key.startswith('level_')})
        elif self.stage == 'build':
            buttons.update({key: self.layout[key] for key in ('undo', 'reset', 'check')})
        else:
            buttons.update({key: self.layout[key] for key in ('view_reset', 'replay')})
        return buttons

    def hit_test(self, point):
        for key, rect in self._buttons().items():
            if inside(rect, point):
                return 'button', key
        if self.stage == 'build':
            if inside(self.layout['palette'], point) and len(self.board) < CAPACITY:
                return 'drag', ('new', None)
            bid = self.scene.pick(*point) if self.scene is not None else None
            if bid is not None and self.board.is_top(bid):
                return 'drag', ('move', bid)
        elif self.stage == 'observe':
            bid = self.scene.pick(*point) if self.scene is not None else None
            return ('select', bid) if bid is not None else ('empty', None)
        return 'empty', None

    def observe(self, detections, now):
        self.detections = tuple(detections)
        events = self.controls.observe(self.detections, self.width, self.height, now,
                                      self.hit_test, allow_zoom=self.stage == 'observe',
                                      allow_palm_rotate=self.stage == 'observe')
        self.handle_events(events, now)
        return events

    def _snap(self, point):
        self.preview = None
        if self.scene is None or not inside(self.layout['build'], point):
            return
        heights = {(x, y): self.board.height_at(x, y) for x in range(WIDTH) for y in range(DEPTH)}
        if self.carry and self.carry[0] == 'move':
            block = self.board.block_by_id(self.carry[1])
            if block:
                heights[block.x, block.y] -= 1
        cell = self.scene.snap_cell(*point, heights=heights, radius=.78)
        if cell is not None and heights[cell] < MAX_HEIGHT:
            self.preview = (*cell, heights[cell])

    def handle_events(self, events, now):
        for event in events:
            kind, target = event['kind'], event.get('target')
            if kind == 'click':
                intent = event['intent']
                if intent == 'select' and self.stage == 'observe':
                    self.selected_id = target
                elif intent == 'empty' and self.stage == 'observe':
                    self.selected_id = None
                elif intent == 'button':
                    if target == 'close':
                        if self.stage == 'levels':
                            self.quit_requested = True
                        else:
                            self.go_back()
                    elif target.startswith('level_'):
                        self.choose_level(target[6:])
                    elif target == 'undo' and self.stage == 'build':
                        self.board.undo()
                        self.check_result = None
                    elif target == 'reset' and self.stage == 'build':
                        self.board.reset()
                        self.check_result = None
                    elif target == 'check':
                        self.check(now)
                    elif target == 'view_reset' and self.stage == 'observe':
                        self.reset_view()
                    elif target == 'replay' and self.stage == 'observe':
                        self.choose_level(self.level.id)
            elif kind == 'drag_start' and self.stage == 'build':
                self.carry = target
                self._snap(event['point'])
            elif kind == 'drag_move' and self.carry is not None:
                self._snap(event['point'])
            elif kind == 'drag_end' and self.carry is not None:
                if self.preview is not None:
                    x, y, _ = self.preview
                    if self.carry[0] == 'new':
                        self.board.add(x, y)
                    else:
                        self.board.move_top(self.carry[1], x, y)
                    self.check_result = None
                self.carry = self.preview = None
            elif kind == 'rotate_move' and self.stage == 'observe':
                dx, dy = event['delta']
                # Camera-basis yaw is inverse to a world-space model yaw:
                # left palm motion => clockwise when viewed from above.
                # Neither axis has a pole clamp or a 0/360 discontinuity.
                # Preserve signed travel so a fast/multiple-turn input cannot
                # make the interpolator take an unintended shorter path.
                self.target_yaw -= dx * 360.0 / self.width
                self.target_pitch += dy * 120.0 / self.height
            elif kind == 'zoom_move' and self.stage == 'observe':
                self.target_scale = float(np.clip(self.target_scale * event['ratio'], self.MIN_SCALE, self.MAX_SCALE))
            elif kind == 'cancel':
                # Watchdog/jumps cancel a carried edit; never commit a stale target.
                self.carry = self.preview = None

    def tick(self, now):
        self.handle_events(self.controls.tick(now), now)
        dt = min(.1, max(0, now - self.last_tick)) if self.last_tick is not None else 0
        self.last_tick = now
        rate = 1 - math.exp(-12 * dt)
        self.yaw += (self.target_yaw-self.yaw)*rate
        self.pitch += (self.target_pitch-self.pitch)*rate
        self.scale += (self.target_scale-self.scale)*rate

    def render(self, background, now=None):
        """Return a newly composed BGR frame without changing the source buffer."""
        started = time.perf_counter()
        now = started if now is None else now
        self.tick(now)
        image = background.copy()
        ui, lo = self.ui, self.layout
        ui.panel(image, lo['panel'])
        ui.button(image, lo['close'], 'close')
        self.visible_controls = tuple(self._buttons())
        if self.stage == 'levels':
            ui.text(image, '空间构建', (int(.1*self.width), int(.23*self.height)), size=int(.044*self.height))
            for i, level in enumerate(LEVELS):
                rect = lo['level_'+level.id]
                if level.id == self.selected_level_id:
                    ui.panel(image, rect, color=(89,94,98), alpha=.72)
                else:
                    ui.panel(image, rect)
                cx = (rect[0]+rect[2])//2
                ui.text(image, str(i+1), (cx, rect[1]+int(.035*self.height)), size=int(.075*self.height), align='center')
                ui.text(image, level.name, (cx, rect[1]+int(.16*self.height)), size=int(.033*self.height), align='center')
        else:
            ui.text(image, self.level.name, (int(.09*self.width), int(.19*self.height)), size=int(.037*self.height))
            if self.stage == 'build':
                result = self.check_result
                self.projection_stage.draw(image, self.build_projection, self.level.id, result=result)
                ui.text(image, '%02d / 24  ·  5 × 4 × 5' % len(self.board),
                        (int(.91*self.width), int(.2*self.height)), size=int(.022*self.height), align='right')
                displayed = self.board.blocks
                if self.carry and self.carry[0] == 'move':
                    displayed = tuple(b for b in displayed if b.id != self.carry[1])
                self.scene = self.renderer.render(image, displayed, lo['build'], grid=False,
                                                  preview=self.preview, pivot=self.build_projection.pivot,
                                                  unit_px=self.build_projection.unit,
                                                  yaw_deg=self.INITIAL_YAW, pitch_deg=self.INITIAL_PITCH,
                                                  height_limit=MAX_HEIGHT)
                for label, world in (('正视图',(WIDTH*.5,DEPTH+GAP,MAX_HEIGHT+.12)),
                                     ('左视图',(-GAP,DEPTH*.5,MAX_HEIGHT+.12)),
                                     ('俯视图',(WIDTH*.5,0,FLOOR_Z))):
                    point = self.build_projection.project(world)
                    point[1] += 13 if label == '俯视图' else -21
                    ui.text(image,label,point,size=max(13,int(.023*self.height)),align='center',color=(218,221,223))
                ui.button(image, lo['palette'], 'cube', active=self.carry is not None)
                for key in ('undo', 'reset', 'check'):
                    ui.button(image, lo[key], key)
            else:
                self.scene = self.renderer.render(image, self.board.blocks, lo['observe'],
                                                  yaw_deg=self.yaw, pitch_deg=self.pitch, scale=self.scale,
                                                  selected_id=self.selected_id, grid=False)
                ui.palm_hint(image, lo['palm_hint'], active=self.controls.active_intent == 'rotate')
                ui.button(image, lo['view_reset'], 'reset')
                ui.button(image, lo['replay'], 'replay')
            if self.feedback and now < self.feedback_until and self.stage == 'build':
                ui.text(image, self.feedback, (int(.39*self.width), int(.84*self.height)), size=int(.024*self.height))
        if self.controls.pointer is not None:
            point = tuple(int(v) for v in self.controls.pointer)
            cv2.circle(image, point, max(3, int(self.height*.006)), (204, 210, 212), 1, cv2.LINE_AA)
        if self.debug:
            for detection in self.detections:
                x1,y1,x2,y2 = detection.box
                cv2.rectangle(image, (x1,y1), (x2,y2), (160,160,160), 1)
                cv2.putText(image, '%s %.2f' % (detection.label, detection.confidence), (x1,max(15,y1-4)),
                            cv2.FONT_HERSHEY_SIMPLEX, .45, (230,230,230), 1, cv2.LINE_AA)
        self.render_count += 1
        self.last_render_ms = (time.perf_counter()-started)*1000
        return image
