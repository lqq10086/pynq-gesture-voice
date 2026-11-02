"""Three construction levels, deliberately without the observation stage.

FourLevelsApp retains the existing entry-point name so capture, inference and
UI-owner scheduling are reused unchanged. No town object or scene is created.
"""
import math
from pathlib import Path
import time

import cv2
from spatial_build.app import SpatialApp, inside
from spatial_build.model import LEVELS, LEVEL_BY_ID, CAPACITY, WIDTH, DEPTH, MAX_HEIGHT
from spatial_build.projection_stage import GAP, FLOOR_Z
from .flat_ui import FlatUi


class BuildOnlySpatialApp(SpatialApp):
    """Keep the original legal edit/gesture pipeline and stop after checking."""
    def __init__(self, width=1280, height=720, font_path=None, voice_direct_entry=False):
        super().__init__(width, height, font_path=font_path,
                         voice_direct_entry=voice_direct_entry)
        self.ui = FlatUi(width, height, font_path=font_path)
        self.placement_enabled = False
        self.placement_target = self.placement_arm_sequence = None
        self.placement_started_at = None

    def clear_placement(self):
        self.placement_target = self.placement_arm_sequence = None
        self.preview = None

    def _layout(self):
        layout = super()._layout()
        layout.update(build=self._rect(.20, .12, .94, .94),
                      palette=self._rect(.10, .28, .185, .42),
                      undo=self._rect(.10, .49, .185, .61),
                      reset=self._rect(.10, .65, .185, .77),
                      check=self._rect(.10, .81, .185, .93))
        return layout

    def _switch(self, stage):
        if stage not in ('levels', 'build'):
            raise ValueError('test_1 only provides level selection and construction')
        self.placement_enabled = False
        self.placement_started_at = None
        self.clear_placement()
        super()._switch(stage)

    def _buttons(self):
        buttons = super()._buttons()
        if self.stage == 'build':
            buttons['place_toggle'] = self.layout['palette']
        return buttons

    def placement_candidate(self, points, preferred=None):
        if self.scene is None or self.level is None or len(self.board) >= CAPACITY:
            return None
        heights = {(x, y): self.board.height_at(x, y)
                   for x in range(WIDTH) for y in range(DEPTH)}
        candidates = [cell for cell in sorted(self.level.coords,
                      key=lambda cell: (cell[2], -cell[1], cell[0])) if heights[cell[:2]] == cell[2]]
        if preferred in candidates:
            candidates.remove(preferred)
            candidates.insert(0, preferred)
        for x, y, z in candidates:
            projected = self.scene.project((x+.5, y+.5, z))
            if all(math.dist(projected, point) <= self.scene.unit*2. for point in points):
                return x, y, z
        return None

    def _placement_cell(self, point):
        preferred = self.placement_target
        target = self.controls.target
        if preferred is None and isinstance(target, tuple) and len(target) == 3 and target[0] == 'place':
            preferred = (*target[1:], self.board.height_at(*target[1:]))
        candidate = self.placement_candidate((point,), preferred)
        return candidate[:2] if candidate is not None else None

    def hit_test(self, point):
        for key, rect in self._buttons().items():
            if inside(rect, point):
                return 'button', key
        if self.stage != 'build':
            return 'empty', None
        if self.placement_enabled:
            cell = self._placement_cell(point) if len(self.board) < CAPACITY else None
            self.preview = (*cell, self.board.height_at(*cell)) if cell is not None else None
            return ('place', ('place', *cell)) if cell is not None else ('empty', None)
        # With placement disabled, only existing top blocks can be dragged.
        bid = self.scene.pick(*point) if self.scene is not None else None
        return ('drag', ('move', bid)) if bid is not None and self.board.is_top(bid) else ('empty', None)

    def handle_events(self, events, now):
        for event in events:
            kind, target = event.get('kind'), event.get('target')
            if kind == 'click' and event.get('intent') == 'button' and target == 'place_toggle':
                if self.stage == 'build':
                    self.placement_enabled = not self.placement_enabled
                    self.carry = None
                    self.clear_placement()
                    self.placement_started_at = float(now) if self.placement_enabled else None
                    # Once enabled, the continuing hand can enter a cell and
                    # start its own two-sample placement confirmation.
                    self.controls.reset(require_release=not self.placement_enabled)
            elif kind == 'place_release' and event.get('intent') == 'place':
                if (self.stage == 'build' and self.placement_enabled
                        and isinstance(target, tuple) and len(target) == 3
                        and target[0] == 'place'):
                    if self.board.add(target[1], target[2]) is not None:
                        self.check_result = None
                    self.clear_placement()
                # A held confirmed pinch only previews; fresh release commits.
            elif kind == 'drag_start':
                if (self.stage == 'build' and not self.placement_enabled
                        and isinstance(target, tuple) and len(target) == 2
                        and target[0] == 'move' and self.board.is_top(target[1])):
                    super().handle_events((event,), now)
                # Old palette/new-block drag events cannot bypass this tool.
            elif kind == 'click' and event.get('intent') == 'button' and target == 'reset':
                self.placement_enabled = False
                self.placement_started_at = None
                self.carry = None
                self.clear_placement()
                self.controls.reset(require_release=True)
                super().handle_events((event,), now)
            elif kind == 'click' and event.get('intent') == 'button' and target == 'undo':
                self.clear_placement()
                self.placement_started_at = float(now) if self.placement_enabled else None
                super().handle_events((event,), now)
            else:
                super().handle_events((event,), now)

    def check(self, now=None):
        if self.stage != 'build':
            return None
        self.clear_placement()
        self.placement_started_at = (time.perf_counter() if now is None else float(now)) if self.placement_enabled else None
        self.check_result = self.board.judge(self.level)
        self._feedback('已通过' if self.check_result.passed else '检查投影',
                       time.perf_counter() if now is None else now)
        # Preserve the player's coordinates/IDs and construction tools on pass.
        return self.check_result

    def render(self, image, now=None, include_projection=True):
        """Draw once into the UI owner's black canvas; camera is never read."""
        started = time.perf_counter()
        now = started if now is None else now
        ui, lo = self.ui, self.layout
        ui.button(image, lo['close'], 'close')
        self.visible_controls = tuple(self._buttons())
        if self.stage == 'levels':
            ui.text(image, '空间构建', (int(.1*self.width), int(.23*self.height)),
                    size=int(.044*self.height))
            for i, level in enumerate(LEVELS):
                rect = lo['level_'+level.id]
                ui.panel(image, rect, color=(112, 112, 112) if level.id == self.selected_level_id else (72, 72, 72))
                cx = (rect[0]+rect[2])//2
                ui.text(image, str(i+1), (cx, rect[1]+int(.035*self.height)),
                        size=int(.075*self.height), align='center')
                ui.text(image, level.name, (cx, rect[1]+int(.16*self.height)),
                        size=int(.033*self.height), align='center')
        else:
            ui.text(image, self.level.name, (int(.09*self.width), int(.19*self.height)),
                    size=int(.037*self.height))
            if include_projection:
                self.projection_stage.draw(image, self.build_projection, self.level.id, result=self.check_result)
            ui.text(image, '%02d / 24' % len(self.board),
                    (int(.91*self.width), int(.2*self.height)), size=int(.022*self.height), align='right')
            displayed = self.board.blocks
            if self.carry and self.carry[0] == 'move':
                displayed = tuple(b for b in displayed if b.id != self.carry[1])
            self.scene = self.renderer.render(image, displayed, lo['build'], grid=False,
                    preview=self.preview, pivot=self.build_projection.pivot,
                    unit_px=self.build_projection.unit,
                    yaw_deg=self.INITIAL_YAW, pitch_deg=self.INITIAL_PITCH,
                    height_limit=MAX_HEIGHT)
            for label, world in (('正视图', (WIDTH*.5, DEPTH+GAP, MAX_HEIGHT+.12)),
                                 ('左视图', (-GAP, DEPTH*.5, MAX_HEIGHT+.12)),
                                 ('俯视图', (WIDTH*.5, 0, FLOOR_Z))):
                point = self.build_projection.project(world)
                point[1] += 13 if label == '俯视图' else -21
                ui.text(image, label, point, size=max(13, int(.023*self.height)), align='center')
            ui.button(image, lo['palette'], 'cube', active=self.placement_enabled)
            ui.text(image, '放置 开' if self.placement_enabled else '放置 关',
                    ((lo['palette'][0]+lo['palette'][2])//2, lo['palette'][3]+5),
                    size=max(12, int(.020*self.height)), align='center')
            for key in ('undo', 'reset', 'check'):
                ui.button(image, lo[key], key)
            if self.feedback and now < self.feedback_until:
                ui.text(image, self.feedback, (int(.39*self.width), int(.84*self.height)),
                        size=int(.024*self.height))
        if self.controls.pointer is not None:
            point = tuple(int(v) for v in self.controls.pointer)
            cv2.circle(image, point, max(3, int(self.height*.006)), (204, 210, 212), 1, cv2.LINE_AA)
        if self.debug:
            for d in self.detections:
                x1,y1,x2,y2 = d.box
                cv2.rectangle(image, (x1,y1), (x2,y2), (160,160,160), 1)
                cv2.putText(image, '%s %.2f' % (d.label,d.confidence), (x1,max(15,y1-4)),
                            cv2.FONT_HERSHEY_SIMPLEX, .45, (230,230,230), 1, cv2.LINE_AA)
        self.render_count += 1
        self.last_render_ms = (time.perf_counter()-started)*1000.
        return image


class FourLevelsApp:
    """Compatibility factory for the unchanged ApplicationPipeline/UIWorker."""
    def __init__(self, width=1280, height=720, root=None, font_path=None,
                 work_path=None):
        from .render import CombinedRenderer
        self.width, self.height = int(width), int(height)
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.work_path = Path(work_path) if work_path is not None else self.root/'works/latest.json'
        self.spatial = BuildOnlySpatialApp(self.width, self.height, font_path=font_path,
                                           voice_direct_entry=True)
        self.mode, self.mode_revision = 'spatial', 0
        self.selected_entry = 1
        self.layout = self.spatial.layout
        self.level_rects = {'level_'+level.id: self.layout['level_'+level.id] for level in LEVELS}
        self._last_observation = None
        self._last_inference_sequence = None
        self._place_toggle_held = False
        self._place_toggle_sequence = None
        self._placement_cycle_seen = 0
        self._last_placement_event = None
        self._fresh_count = self._stale_count = 0
        self._skip_post_prepare_animation_once = False
        self.render_count, self.last_render_ms = 0, 0.
        self.renderer = CombinedRenderer(self)
        self._compat_compositor = None

    @property
    def stage(self):
        return self.spatial.stage

    @property
    def controls(self):
        return self.spatial.controls

    @property
    def selected_id(self):
        return self.spatial.selected_id

    @selected_id.setter
    def selected_id(self, value):
        self.spatial.selected_id = value

    @property
    def debug(self):
        return self.spatial.debug

    @debug.setter
    def debug(self, value):
        self.spatial.debug = bool(value)

    @property
    def quit_requested(self):
        return self.spatial.quit_requested

    @quit_requested.setter
    def quit_requested(self, value):
        self.spatial.quit_requested = bool(value)

    @property
    def metrics(self):
        return dict(mode='construction_only', stage=self.stage,
                    selected_entry=self.selected_entry,
                    fresh_observations=self._fresh_count, ignored_stale=self._stale_count,
                    spatial_blocks=len(self.spatial.board),
                    render_count=self.render_count, last_render_ms=self.last_render_ms,
                    town_loaded=False, observation_enabled=False,
                    placement_enabled=self.spatial.placement_enabled,
                    placement_ready=self.spatial.placement_target,
                    placement_cycle_seen=self._placement_cycle_seen,
                    last_placement_event=self._last_placement_event,
                    placement_assistance='target_only_two_block_radius_bottom_back_left')

    def cancel_interaction(self, now=None):
        self.spatial.carry = self.spatial.preview = None
        self.spatial.clear_placement()
        if self.spatial.placement_enabled:
            self.spatial.placement_started_at = time.perf_counter() if now is None else float(now)
        self.controls.reset(require_release=True)

    def choose_level(self, level_id):
        if level_id not in LEVEL_BY_ID:
            return False
        self.cancel_interaction()
        self.selected_entry = next(i+1 for i, level in enumerate(LEVELS) if level.id == level_id)
        self.spatial.choose_level(level_id)
        return True

    def return_to_levels(self):
        self.cancel_interaction()
        self.spatial._switch('levels')
        return True

    def _switch(self, stage):
        self.cancel_interaction()
        self.spatial._switch(stage)

    def hit_test(self, point):
        return self.spatial.hit_test(point)

    def _control_hit_test(self, point):
        hit = self.hit_test(point)
        if self._place_toggle_held and hit == ('button', 'place_toggle'):
            return None
        return hit

    def observe_sequenced(self, detections, now, sequence):
        """UI mailbox gaps do not revoke an already observed placement."""
        if self._last_inference_sequence is not None and sequence <= self._last_inference_sequence:
            self._stale_count += 1
            return []
        self._last_inference_sequence = sequence
        return self.observe(detections, now)

    def _control_detections(self, detections):
        controlled = []
        value = self.controls._detection_value
        for detection in detections:
            box = value(detection, 'box')
            if value(detection, 'label') != 'pinch_point' or box is None:
                controlled.append(detection)
                continue
            try:
                x1, y1, x2, y2 = map(float, box)
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(v) for v in (x1,y1,x2,y2)) or x2 <= x1 or y2 <= y1:
                continue
            center = (x1+x2)*.5
            fraction = .75 if center < self.width*.5 else .25 if center > self.width*.5 else .5
            controlled.append(dict(label='pinch_point', confidence=value(detection, 'confidence', 0.),
                                   center=(x1+(x2-x1)*fraction, (y1+y2)*.5)))
        return tuple(controlled)

    def _sample_points(self, samples):
        points = []
        for sample in samples:
            pinches = self.controls._points(self._control_detections(sample.detections), 'pinch_point')
            if not pinches:
                return None
            points.append(pinches[0][1])
        return points

    def observe_inference(self, result, now):
        """Consume bounded real NN evidence, including a release hidden by rendering."""
        if self._last_inference_sequence is not None and result.sequence <= self._last_inference_sequence:
            self._stale_count += 1
            return []
        self._last_inference_sequence = result.sequence
        cycle = result.placement_cycle
        if (self._place_toggle_held and self._place_toggle_sequence is not None
                and cycle is not None
                and self._place_toggle_sequence < cycle.release_sequence <= result.sequence):
            self._place_toggle_held = False
        events = self.observe(result.detections, now, _allow_place_release=False)
        game = self.spatial
        active = game.stage == 'build' and game.placement_enabled
        if (game.placement_target is not None
                and (game.placement_target not in game.level.coords
                     or game.board.height_at(*game.placement_target[:2]) != game.placement_target[2])):
            game.clear_placement()
        if cycle is not None and cycle.release_sequence > self._placement_cycle_seen:
            self._placement_cycle_seen = cycle.release_sequence
            self._last_placement_event = dict(sequence=cycle.release_sequence, status='tool_disabled')
            samples = cycle.pinches
            if active:
                self._last_placement_event['status'] = 'before_tool_enable_or_cancel'
            if (active and len(samples) == 2 and game.placement_started_at is not None
                    and all(sample.captured_at >= game.placement_started_at for sample in samples)):
                self._last_placement_event['status'] = 'no_target_within_two_blocks'
                points = self._sample_points(samples)
                target = game.placement_candidate(points, game.placement_target) if points is not None else None
                if (target is not None and target in game.level.coords
                        and game.board.height_at(*target[:2]) == target[2]):
                    before = len(game.board)
                    game.handle_events((dict(kind='place_release', intent='place',
                                      target=('place', *target[:2])),), now)
                    self._last_placement_event.update(target=target,
                        status='placed' if len(game.board) == before+1 else 'board_rejected')
                    self.controls.reset(require_release=False)
        run = result.placement_observations
        if (active and len(run) == 2
                and game.placement_started_at is not None
                and all(sample.captured_at >= game.placement_started_at for sample in run)
                and run[0].sequence > self._placement_cycle_seen):
            points = self._sample_points(run)
            if points is not None:
                target = game.placement_candidate(points, game.placement_target)
                if target is not None:
                    game.placement_target = target
                    game.placement_arm_sequence = run[-1].sequence
                else:
                    game.clear_placement()
        if active:
            game.preview = game.placement_target
        return events

    def observe(self, detections, now, *, _allow_place_release=True):
        now = float(now)
        if not math.isfinite(now):
            raise ValueError('now must be finite')
        if self._last_observation is not None and now <= self._last_observation:
            self._stale_count += 1
            return []
        self._last_observation = now
        self._fresh_count += 1
        detections = tuple(detections)
        controlled = self._control_detections(detections)
        if not self.controls._points(controlled, 'pinch_point'):
            self._place_toggle_held = False
        events = self.controls.observe(controlled, self.width, self.height,
                                       now, self._control_hit_test, allow_zoom=False,
                                       allow_palm_rotate=False)
        if not _allow_place_release:
            events = [event for event in events if event.get('kind') != 'place_release']
        self.spatial.detections = detections
        self.handle_events(events, now)
        if self.spatial.placement_enabled:
            if _allow_place_release:
                target = self.controls.target
                if (self.controls.phase == 'latched' and isinstance(target, tuple)
                        and len(target) == 3 and target[0] == 'place'):
                    self.spatial.placement_target = (*target[1:], self.spatial.board.height_at(*target[1:]))
            self.spatial.preview = self.spatial.placement_target
        return events

    def tick(self, now):
        events = self.spatial.tick(now)
        if self.spatial.placement_enabled and self.spatial.placement_target is not None:
            self.spatial.preview = self.spatial.placement_target
        return events

    def handle_events(self, events, now):
        for event in events:
            if event.get('kind') == 'click' and event.get('intent') == 'button':
                if (event.get('target') == 'place_toggle' and event.get('source') == 'pinch'
                        and self.stage == 'build'):
                    if self._place_toggle_held:
                        continue
                    self._place_toggle_held = True
                    self._place_toggle_sequence = self._last_inference_sequence
                self._button(event.get('target'), now)
            else:
                self.spatial.handle_events((event,), now)

    def _button(self, name, now):
        if not isinstance(name, str):
            return False
        if self.stage == 'levels':
            if name == 'close':
                self.quit_requested = True
                return True
            return self.choose_level(name[6:]) if name.startswith('level_') else False
        if name not in ('close', 'undo', 'reset', 'check', 'place_toggle'):
            return False
        self.spatial.handle_events((dict(kind='click', intent='button', target=name),), now)
        return True

    def go_back(self):
        return self.spatial.go_back()

    def apply_voice_command(self, label, now=None):
        now = time.perf_counter() if now is None else float(now)
        choices = dict(zip(('one', 'two', 'three'), (level.id for level in LEVELS)))
        if label in choices:
            return self.choose_level(choices[label]) if self.stage == 'levels' else False
        if label == 'on' and self.stage == 'levels':
            return self.choose_level(self.spatial.selected_level_id)
        return self.spatial.apply_voice_command(label, now)

    def render(self, bgr, now=None):
        from town_creator.layers import DisplayCompositor
        now = time.perf_counter() if now is None else float(now)
        started = time.perf_counter()
        self.tick(now)
        packet = self.renderer.prepare(self, now, self.render_count+1)
        if self._compat_compositor is None:
            self._compat_compositor = DisplayCompositor(self.width, self.height, 'tint')
        output = bgr.copy()
        self._compat_compositor.compose_into(output, packet)
        self.render_count += 1
        self.last_render_ms = (time.perf_counter()-started)*1000.
        return output
