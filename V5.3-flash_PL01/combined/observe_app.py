"""Independent preset-geometry inspection; the original renderer and gestures remain.

FourLevelsApp is the existing pipeline factory name. This split exposes only
three preset choices and observation, with no construction or town transition.
"""
import math
from pathlib import Path
import time

from spatial_build.app import SpatialApp, inside
from spatial_build.model import Board, LEVELS, LEVEL_BY_ID
from spatial_build.observe_renderer import block_values


class ObserveOnlySpatial(SpatialApp):
    MAX_SCALE = 1.30
    PALM_TIMEOUT_SECONDS = 1.0

    def __init__(self, width=1280, height=720, font_path=None, voice_direct_entry=False):
        from .observe_flat_ui import FlatUi
        super().__init__(width, height, font_path=font_path, voice_direct_entry=voice_direct_entry)
        self.ui = FlatUi(width, height, font_path=font_path)
        from spatial_build.observe_renderer import Renderer
        from spatial_build.observe_controls import GestureController
        self.renderer = Renderer()
        self.controls = GestureController()
        # Use the vertical band between the existing close and palm controls.
        mx = int(width*.16)
        self._base_observe = (mx, int(height*.22), width-mx, height-int(height*.22))
        self.layout['observe'] = (mx, int(height*.145), width-mx, int(height*.80))
        self.observe_unit_px = None
        self.rotation_velocity = (0., 0.)
        self.rotation_stop_reason = 'idle'
        self._rotation_at = self._rotation_until = None
        self._rotation_snap_pending = False
        self.snap_count = 0
        self.snap_face = None

    @property
    def continuous_rotating(self):
        return self.rotation_velocity != (0., 0.)

    def stop_continuous_rotation(self, reason='cancel'):
        if self.continuous_rotating:
            self.target_yaw, self.target_pitch = self.yaw, self.pitch
        self.rotation_velocity = (0., 0.)
        self.rotation_stop_reason = reason
        self._rotation_at = self._rotation_until = None
        self._rotation_snap_pending = False

    def snap_to_principal_view(self):
        if self._rotation_until is not None:
            return False
        yaw, pitch = math.radians(self.yaw % 360.), math.radians(self.pitch % 360.)
        direction = (math.sin(yaw)*math.cos(pitch),
                     -math.cos(yaw)*math.cos(pitch), math.sin(pitch))
        axis = max(range(3), key=lambda index: abs(direction[index]))
        if abs(direction[axis]) < math.cos(math.radians(7.))-1e-12:
            return False
        if axis == 2:
            self.pitch = self.target_pitch = 90.+180.*round((self.pitch-90.)/180.)
            self.snap_face = 'top' if direction[axis] > 0 else 'bottom'
        else:
            self.yaw = self.target_yaw = 90.*round(self.yaw/90.)
            self.pitch = self.target_pitch = 180.*round(self.pitch/180.)
            self.snap_face = (('left','right') if axis == 0 else ('front','back'))[direction[axis] > 0]
        self.snap_count += 1
        return True

    def _advance_rotation_pose(self, now):
        if self._rotation_at is None:
            return
        until = min(now,self._rotation_until)
        dt = max(0.,until-self._rotation_at)
        self._rotation_at = until
        self.yaw += self.rotation_velocity[0]*dt
        self.pitch += self.rotation_velocity[1]*dt
        self.target_yaw, self.target_pitch = self.yaw, self.pitch

    def renew_palm_rotation(self, now, deadline):
        self.advance_continuous_rotation(now)
        self._rotation_at = now
        self._rotation_until = deadline

    def pause_continuous_rotation(self, now, reason='deadzone'):
        self._advance_rotation_pose(now)
        self.rotation_velocity = (0.,0.)
        self.rotation_stop_reason = reason

    def start_continuous_rotation(self, velocity, now, deadline):
        self.advance_continuous_rotation(now)
        self._rotation_at = now
        self.rotation_velocity = tuple(velocity)
        self.rotation_stop_reason = None
        self._rotation_until = deadline
        self._rotation_snap_pending |= self.continuous_rotating

    def expire_continuous_rotation(self, now):
        if self._rotation_until is not None and now >= self._rotation_until:
            self._advance_rotation_pose(now)
            snap = self._rotation_snap_pending
            self.stop_continuous_rotation('palm_timeout')
            if snap:
                self.snap_to_principal_view()
            return True
        return False

    def advance_continuous_rotation(self, now):
        if self.expire_continuous_rotation(now) or self._rotation_until is None:
            return
        self._advance_rotation_pose(now)

    def reset_view(self):
        self.stop_continuous_rotation('reset')
        super().reset_view()

    def _switch(self, stage):
        if stage not in ('levels', 'observe'):
            raise ValueError('test_2 supports only levels and observe')
        self.stop_continuous_rotation('stage_change')
        super()._switch(stage)

    def hit_test(self, point):
        if self.stage != 'observe' or not getattr(self, '_display_picking_locked', False):
            return super().hit_test(point)
        for name in ('close', 'view_reset', 'replay'):
            if inside(self.layout[name], point): return 'button', name
        scene = getattr(self, 'pinch_scene', None) or getattr(self, 'displayed_scene', None)
        bid = scene.pick(*point) if scene is not None else None
        return ('select', bid) if bid is not None else ('empty', None)

    def tick_controls(self, now):
        pending_palm = self.controls.phase == 'pending' and self.controls._source == 'palm'
        last_observation = self.controls._last_observation
        events = self.controls.tick(now)
        # The shared controller emits no event when an unconfirmed palm expires.
        if (pending_palm and not events and self.controls.phase == 'blocked'
                and last_observation is not None
                and now-last_observation >= self.controls.watchdog_seconds):
            events = [dict(kind='cancel',reason='watchdog',source='palm',intent='rotate',time=now)]
        return events

    def tick(self, now):
        events = self.tick_controls(now)
        self.handle_events(events, now)
        self.advance_continuous_rotation(now)
        elapsed = max(0., now-self.last_tick) if self.last_tick is not None else 0.
        self.last_tick = now
        rate = -math.expm1(-12.*elapsed)
        for name, tolerance in (('yaw', .05), ('pitch', .05), ('scale', .001)):
            target = getattr(self, 'target_'+name)
            value = getattr(self, name)+(target-getattr(self, name))*rate
            setattr(self, name, target if abs(target-value) <= tolerance else value)
        return events

    def choose_level(self, level_id, coords=None):
        self.level = LEVEL_BY_ID[level_id]
        self.selected_level_id = level_id
        self.board = Board.from_coords(self.level.coords if coords is None else coords)
        # Preserve the original 1x size; only the available rotation band grows.
        values = [block_values(block) for block in self.board.blocks]
        _, self.observe_unit_px = self.renderer._fit(values,self._base_observe,(5,4),False,4,None)
        self.selected_id = self.check_result = None
        self.feedback = ''
        self.yaw = self.target_yaw = self.INITIAL_YAW
        self.pitch = self.target_pitch = self.INITIAL_PITCH
        self.scale = self.target_scale = 1.0
        self.last_tick = None
        self._switch('observe')

    def go_back(self):
        if self.stage != 'observe':
            return False
        self.selected_id = None
        self._switch('levels')
        return True

    def check(self, now=None):
        return None

    def handle_events(self, events, now):
        # Replay reuses choose_level above and cannot reopen a build page.
        allowed = {'close', 'view_reset', 'replay'}
        allowed.update('level_' + level.id for level in LEVELS)
        filtered = []
        for event in events:
            kind = event.get('kind')
            if kind in ('cancel', 'rotate_end'):
                self.stop_continuous_rotation(event.get('reason',kind))
            if kind == 'click' and event.get('intent') == 'button':
                target = event.get('target')
                if target not in allowed:
                    continue
                if target.startswith('level_') and self.stage != 'levels':
                    continue
            elif kind not in ('click', 'rotate_move', 'zoom_move', 'cancel'):
                continue
            filtered.append(event)
        super().handle_events(filtered, now)

    def apply_voice_command(self, label, now=None):
        choices = dict(zip(('one', 'two', 'three'), (level.id for level in LEVELS)))
        if self.stage == 'levels' and (label in choices or label == 'on'):
            self.choose_level(choices.get(label, self.selected_level_id))
            return True
        if label in ('off', 'back'):
            return self.go_back()
        return False


class FourLevelsApp:
    """Stable ApplicationPipeline/UIWorker protocol for the inspection split."""
    def __init__(self, width=1280, height=720, root=None, font_path=None, work_path=None):
        from .observe_render import CombinedRenderer
        self.width, self.height = int(width), int(height)
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.font_path, self.work_path = font_path, work_path
        self.spatial = ObserveOnlySpatial(self.width, self.height, font_path=font_path,
                                          voice_direct_entry=True)
        self.mode, self.mode_revision, self.selected_entry = 'spatial', 0, 1
        self._last_observation = None
        self._fresh_count = self._stale_count = 0
        self._skip_post_prepare_animation_once = False
        self._displayed_scene_mode_revision = None
        self._displayed_geometry_at = None
        self._displayed_pose = None
        self._ignored_pre_display_results = 0
        self._require_pinch_release = False
        self._rotation_sample = None
        self._rotation_axis = None
        self._last_sample_time = None
        self._last_rotation_sample = None
        self._palm_watchdog_restart_pending = False
        self._palm_watchdog_restarts = 0
        self._rotation_last_cancel_reason = self._rotation_last_cancel_source = None
        self.render_count, self.last_render_ms = 0, 0.0
        self.layout = dict(panel=self.spatial.layout['panel'], close=self.spatial.layout['close'])
        self.level_rects = {'level_' + level.id: self.spatial.layout['level_' + level.id]
                            for level in LEVELS}
        self.layout.update(self.level_rects)
        self.renderer = CombinedRenderer(self)
        self._compat_compositor = None

    @property
    def stage(self): return self.spatial.stage
    @property
    def controls(self): return self.spatial.controls
    @property
    def selected_id(self): return self.spatial.selected_id
    @selected_id.setter
    def selected_id(self, value): self.spatial.selected_id = value
    @property
    def debug(self): return self.spatial.debug
    @debug.setter
    def debug(self, value): self.spatial.debug = bool(value)
    @property
    def quit_requested(self): return self.spatial.quit_requested
    @quit_requested.setter
    def quit_requested(self, value): self.spatial.quit_requested = bool(value)
    @property
    def metrics(self):
        return dict(mode='observe_only', stage=self.stage, selected_entry=self.selected_entry,
                    mode_revision=self.mode_revision, fresh_observations=self._fresh_count,
                    ignored_stale=self._stale_count, render_count=self.render_count,
                    last_render_ms=self.last_render_ms, spatial_blocks=len(self.spatial.board),
                    ignored_pre_display_results=self._ignored_pre_display_results,
                    pinch_pose_frozen=getattr(self.spatial, 'pinch_scene', None) is not None,
                    entry_pinch_release_required=self._require_pinch_release,
                    continuous_rotating=self.spatial.continuous_rotating,
                    rotation_velocity=self.spatial.rotation_velocity,
                    rotation_angle_velocity=self.spatial.rotation_velocity,
                    rotation_stop_reason=self.spatial.rotation_stop_reason,
                    rotation_deadline=self.spatial._rotation_until,
                    rotation_axis=self._rotation_axis,
                    view_snap_count=self.spatial.snap_count,
                    view_snap_face=self.spatial.snap_face,
                    rotation_last_sample=self._last_rotation_sample,
                    rotation_last_cancel_reason=self._rotation_last_cancel_reason,
                    rotation_last_cancel_source=self._rotation_last_cancel_source,
                    palm_watchdog_restart_pending=self._palm_watchdog_restart_pending,
                    palm_watchdog_restarts=self._palm_watchdog_restarts,
                    preset_geometry=True, construction_enabled=False, town_enabled=False)

    def _stop_rotation(self, reason, reset_palm=False):
        self.spatial.stop_continuous_rotation(reason)
        self._rotation_sample = None
        self._rotation_axis = None
        if reset_palm and self.controls._source == 'palm':
            self.controls.reset(require_release=False)

    def _expire_rotation(self, now):
        if self.spatial.expire_continuous_rotation(now):
            self._stop_rotation('palm_timeout',reset_palm=True)

    def cancel_interaction(self, now=None):
        self._stop_rotation('cancel')
        self._palm_watchdog_restart_pending = False
        self.spatial.carry = self.spatial.preview = None
        self.spatial.pinch_scene = None
        self.controls.reset(require_release=True)

    def choose_level(self, level_id):
        if level_id not in LEVEL_BY_ID: return False
        self.cancel_interaction()
        self.selected_entry = next(i+1 for i, level in enumerate(LEVELS) if level.id == level_id)
        construction = getattr(self, '_construction', None)
        coords = construction[1] if construction is not None and construction[0] == level_id else None
        self.spatial.choose_level(level_id, coords)
        self.mode_revision += 1
        self._await_entry_pinch_release()
        return True

    def load_construction(self, level_id, coords):
        self._construction = level_id, tuple(coords)
        return self.choose_level(level_id)

    def _await_entry_pinch_release(self):
        # Opening the selecting hand may start rotation immediately. The held
        # selecting pinch itself cannot pick an object in the new view.
        self.controls.reset(require_release=False)
        self._require_pinch_release = True

    def return_to_levels(self):
        self.cancel_interaction()
        self.spatial.selected_id = None
        self.spatial._switch('levels')
        self.mode_revision += 1
        return True

    def _switch(self, stage):
        if stage == 'levels': return self.return_to_levels()
        if stage == 'observe' and self.spatial.level is not None:
            self.cancel_interaction()
            self.spatial._switch('observe')
            self.mode_revision += 1
            self._await_entry_pinch_release()
            return True
        raise ValueError('test_2 supports only levels and preset observation')

    def hit_test(self, point):
        if self.stage == 'levels':
            if inside(self.layout['close'], point): return 'button', 'close'
            for name, rect in self.level_rects.items():
                if inside(rect, point): return 'button', name
            return 'empty', None
        return self.spatial.hit_test(point)

    def set_displayed_scene(self, scene, revision, presented_at, mode_revision=None, pose=None):
        mode_revision = self.mode_revision if mode_revision is None else mode_revision
        if mode_revision != self.mode_revision: return False
        if (getattr(self.spatial, 'displayed_scene', None) is not scene
                or self._displayed_scene_mode_revision != mode_revision):
            self._displayed_geometry_at = presented_at
        self.spatial.displayed_scene = scene
        self.spatial._display_picking_locked = True
        self._displayed_scene_mode_revision = mode_revision
        self._displayed_pose = pose if pose is not None else (self.spatial.yaw, self.spatial.pitch, self.spatial.scale)
        return True

    def _valid_palm_point(self, point):
        return (point is not None and all(math.isfinite(value) for value in point)
                and 0 <= point[0] < self.width and 0 <= point[1] < self.height)

    def _stale_observation(self, now, sample_time):
        return ((self._last_observation is not None and now <= self._last_observation)
                or (self._last_sample_time is not None and sample_time <= self._last_sample_time))

    def _keeps_palm_rotation(self, detections):
        palms = self.controls._points(detections,'open_palm')
        return (self.controls._source == 'palm' and self.controls.phase in ('pending','active')
                and len(palms) == 1 and self._valid_palm_point(palms[0][1]))

    def _palm_jump_fraction(self, detections, sample_time):
        original = self.controls.maximum_jump_fraction
        if not self._keeps_palm_rotation(detections) or self._rotation_sample is None:
            return original
        point = self.controls._points(detections,'open_palm')[0][1]
        previous, previous_time = self._rotation_sample
        dt = sample_time-previous_time
        if not 0 < dt <= self.spatial.PALM_TIMEOUT_SECONDS:
            return original
        diagonal = math.hypot(self.width,self.height)
        base = original*diagonal
        dx, dy = abs(point[0]-previous[0]), abs(point[1]-previous[1])
        # Keep the existing hand-motion jump budget when rotation sensitivity changes.
        if dx <= max(base,2.*self.width*dt) and dy <= max(base,4.*self.height*dt/3.):
            return max(original,math.hypot(dx,dy)/diagonal+1e-12)
        return original

    def _record_rotation_end(self, event):
        if event.get('reason') == 'palm_timeout':
            self.spatial.expire_continuous_rotation(event['time'])
        self._stop_rotation(event.get('reason',event['kind']))
        if event['kind'] == 'cancel':
            self._rotation_last_cancel_reason = event.get('reason')
            self._rotation_last_cancel_source = event.get('source')
            self._palm_watchdog_restart_pending = (event.get('reason') == 'watchdog'
                and event.get('source') == 'palm' and event.get('intent') == 'rotate')
        else:
            self._palm_watchdog_restart_pending = False

    def can_observe_without_display(self, detections):
        return (self.stage == 'observe'
                and self._displayed_scene_mode_revision == self.mode_revision
                and (self._require_pinch_release or self._keeps_palm_rotation(detections)
                     or not self.controls._points(detections, 'pinch_point')))

    def pause_unpresented_input(self, detections, now):
        if (self.stage == 'observe' and not self._require_pinch_release
                and self.controls._points(detections,'pinch_point')
                and not self._keeps_palm_rotation(detections)):
            self._stop_rotation('unpresented_pinch')
            return True
        return False

    def observe_inference(self, result, now):
        now, sample_time = float(now), float(result.captured_at)
        if not math.isfinite(now) or not math.isfinite(sample_time): raise ValueError('observation times must be finite')
        if self._stale_observation(now,sample_time):
            self._stale_count += 1
            return []
        pinches = (() if self._require_pinch_release or self._keeps_palm_rotation(result.detections)
                   else self.controls._points(result.detections, 'pinch_point'))
        if self.stage == 'observe' and pinches:
            if self._displayed_scene_mode_revision != self.mode_revision or self.spatial.displayed_scene is None:
                self._ignored_pre_display_results += 1
                self.pause_unpresented_input(result.detections,now)
                return []
            self._stop_rotation('pinch',reset_palm=True)
            if getattr(self.spatial, 'pinch_scene', None) is None:
                # Pick the display visible when this result is processed. This
                # deliberately avoids historical camera-time pose tracking.
                self.spatial.pinch_scene = self.spatial.displayed_scene
                for name, value in zip(('yaw', 'pitch', 'scale'), self._displayed_pose):
                    setattr(self.spatial, name, value)
                    setattr(self.spatial, 'target_'+name, value)
        events = self.observe(result.detections, now, sample_time=sample_time)
        if not pinches and self.controls._source != 'pinch': self.spatial.pinch_scene = None
        return events

    def observe(self, detections, now, sample_time=None):
        now = float(now)
        sample_time = now if sample_time is None else float(sample_time)
        if not math.isfinite(now) or not math.isfinite(sample_time): raise ValueError('observation times must be finite')
        if self._stale_observation(now,sample_time):
            self._stale_count += 1
            return []
        self._last_observation = now
        self._last_sample_time = sample_time
        self._fresh_count += 1
        detections = tuple(detections)
        self.spatial.detections = detections
        controlled = detections
        invalid_palm = any(self.controls._detection_value(detection,'label') == 'open_palm'
            and not self._valid_palm_point(self.controls._center(detection)) for detection in detections)
        if invalid_palm:
            controlled = tuple(detection for detection in controlled
                               if self.controls._detection_value(detection,'label') != 'open_palm')
        if now-sample_time >= self.spatial.PALM_TIMEOUT_SECONDS:
            controlled = tuple(detection for detection in controlled
                               if self.controls._detection_value(detection,'label') != 'open_palm')
        if self.stage == 'observe' and self._require_pinch_release:
            if self.controls._points(detections, 'pinch_point'):
                controlled = tuple(detection for detection in controlled
                                   if self.controls._detection_value(detection, 'label') != 'pinch_point')
            else:
                self._require_pinch_release = False
        if (self.controls._source == 'palm'
                and not self.controls._points(controlled,'open_palm')
                and self.controls._points(controlled,'pinch_point')):
            self._stop_rotation('pinch',reset_palm=True)
        if len(self.controls._points(controlled,'open_palm')) > 1:
            self._stop_rotation('two_palms')
        self._expire_rotation(now)
        events = self.spatial.tick_controls(now)
        self.handle_events(events, now)
        palms = self.controls._points(controlled,'open_palm')
        if (self._palm_watchdog_restart_pending and self.controls.phase == 'blocked'
                and len(palms) == 1 and not self.controls._points(controlled,'pinch_point')):
            self.controls.reset(require_release=False)
            self._palm_watchdog_restart_pending = False
            self._palm_watchdog_restarts += 1
            self._stop_rotation('watchdog_restart')
        original_jump_fraction = self.controls.maximum_jump_fraction
        self.controls.maximum_jump_fraction = self._palm_jump_fraction(controlled,sample_time)
        try:
            observed = self.controls.observe(controlled, self.width, self.height, now, self.hit_test,
                allow_zoom=self.stage == 'observe', allow_palm_rotate=self.stage == 'observe',
                sample_time=sample_time)
        finally:
            self.controls.maximum_jump_fraction = original_jump_fraction
        self.handle_events(observed, now)
        events.extend(observed)
        self._update_continuous_rotation(controlled, now, sample_time)
        if invalid_palm: self._stop_rotation('invalid_palm')
        if self.controls.phase != 'blocked': self._palm_watchdog_restart_pending = False
        return events

    def _update_continuous_rotation(self, detections, now, sample_time):
        palms = self.controls._points(detections, 'open_palm')
        if self.stage == 'observe' and not palms and not self.controls._points(detections,'pinch_point'):
            return
        if (self.stage != 'observe' or len(palms) != 1
                or self.controls._source != 'palm' or self.controls.phase not in ('pending','active')):
            reason = ('stage_change' if self.stage != 'observe' else
                      'no_palm' if not palms else 'two_palms' if len(palms)>1 else
                      'source_change' if self.controls._source != 'palm' else 'controller_'+self.controls.phase)
            self._stop_rotation(reason)
            return
        point = palms[0][1]
        previous, self._rotation_sample = self._rotation_sample, (point, sample_time)
        self._last_rotation_sample = dict(point=point,sample_time=sample_time,observed_at=now)
        deadline = now+self.spatial.PALM_TIMEOUT_SECONDS
        self.spatial.renew_palm_rotation(now, deadline)
        if previous is None:
            self.spatial.pause_continuous_rotation(now,'awaiting_sample')
            return
        if self.controls.phase != 'active': return
        dt = sample_time-previous[1]
        dx, dy = point[0]-previous[0][0], point[1]-previous[0][1]
        if self._rotation_axis is None and (abs(dx)>self.width*3./1280. or abs(dy)>self.height*3./720.):
            self._rotation_axis = 'horizontal' if abs(dx)>=abs(dy) else 'vertical'
        velocity = (max(-180.,min(180.,-dx/self.width*120./dt))
                    if self._rotation_axis == 'horizontal' and abs(dx)>self.width*3./1280. else 0.,
                    max(-120.,min(120.,dy/self.height*120./dt))
                    if self._rotation_axis == 'vertical' and abs(dy)>self.height*3./720. else 0.)
        if velocity == (0.,0.):
            self.spatial.pause_continuous_rotation(now)
        else:
            self.spatial.start_continuous_rotation(velocity, now, deadline)

    def tick(self, now):
        self._expire_rotation(now)
        if self._skip_post_prepare_animation_once:
            self._skip_post_prepare_animation_once = False
            if self.stage == 'observe':
                # Keep the rendered pose stable during UIWorker's safety tick;
                # still advance timeout handling, then resume interpolation.
                events = self.spatial.tick_controls(now)
                self.handle_events(events, now)
                if self.controls.phase in ('idle', 'blocked'): self.spatial.pinch_scene = None
                return events
        events = self.spatial.tick(now)
        for event in events:
            if event.get('kind') in ('cancel','rotate_end'): self._record_rotation_end(event)
        if self.spatial.rotation_stop_reason == 'palm_timeout' and self._rotation_sample is not None:
            self._stop_rotation('palm_timeout',reset_palm=True)
        if self.controls.phase in ('idle', 'blocked'): self.spatial.pinch_scene = None
        return events

    def handle_events(self, events, now):
        for event in events:
            if event.get('kind') in ('cancel','rotate_end'):
                self._record_rotation_end(event)
            # Fresh palm displacement sets velocity; do not also apply its
            # original one-shot angle delta. Explicit fixture moves remain.
            if event.get('kind') == 'rotate_move' and event.get('source') == 'palm':
                continue
            if event.get('kind') == 'click' and event.get('intent') == 'button':
                self._button(event.get('target'), now)
            else:
                self.spatial.handle_events((event,), now)

    def _button(self, name, now):
        if self.stage == 'levels':
            if name == 'close': self.quit_requested = True; return True
            if isinstance(name, str) and name.startswith('level_'): return self.choose_level(name[6:])
            return False
        if name not in ('close', 'view_reset', 'replay'): return False
        if name == 'close': return self.go_back()
        if name == 'replay': return self.choose_level(self.spatial.level.id)
        self.cancel_interaction(now)
        self.spatial.reset_view()
        self.mode_revision += 1
        self._await_entry_pinch_release()
        return True

    def go_back(self):
        if self.stage != 'observe': return False
        return self.return_to_levels()

    def apply_voice_command(self, label, now=None):
        choices = dict(zip(('one', 'two', 'three'), (level.id for level in LEVELS)))
        if self.stage == 'levels' and (label in choices or label == 'on'):
            return self.choose_level(choices.get(label, LEVELS[self.selected_entry-1].id))
        if label in ('off', 'back'): return self.go_back()
        return False

    def render(self, bgr, now=None):
        from town_creator.layers import DisplayCompositor
        now = time.perf_counter() if now is None else float(now)
        started = time.perf_counter()
        self.tick(now)
        packet = self.renderer.prepare(self, now, self.render_count+1)
        if self._compat_compositor is None:
            self._compat_compositor = DisplayCompositor(self.width, self.height, 'tint')
        output = bgr.copy()
        try: self._compat_compositor.compose_into(output, packet)
        finally: self._skip_post_prepare_animation_once = False
        self.render_count += 1
        self.last_render_ms = (time.perf_counter()-started)*1000.0
        return output
