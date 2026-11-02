"""One UI owner switches between accepted construction and native inspection."""
import math
from pathlib import Path
import time

from spatial_build.model import LEVELS
from .app import FourLevelsApp as BuildApp
from .observe_app import FourLevelsApp as ObserveApp
from .voice import valid_voice_payload


class SerialRenderer:
    def __init__(self, owner):
        self.owner = owner

    def visual_key(self, app, now):
        child = self.owner.active
        return (self.owner.module_epoch, self.owner.active_module,
                child.renderer.visual_key(child, now))

    def prepare(self, app, now, revision):
        owner = self.owner
        owner._counts[owner.active_module]['render'] += 1
        return owner.active.renderer.prepare(owner.active, now, revision)

    @property
    def prepared_scene(self):
        return getattr(self.owner.active.renderer, 'prepared_scene', None)

    def release_inactive_scratch(self):
        return self.owner.active.renderer.release_inactive_scratch()

    def metrics(self):
        return dict(self.owner.active.renderer.metrics(),
                    active_module=self.owner.active_module,
                    module_epoch=self.owner.module_epoch)


class SerialApp:
    def __init__(self, width=1280, height=720, root=None, font_path=None, work_path=None):
        self.width, self.height = int(width), int(height)
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.font_path, self.work_path = font_path, work_path
        self.active_module, self.module_epoch = 'build', 0
        self.transition_perf = time.perf_counter()
        self.transition_monotonic = time.monotonic()
        self._require_pinch_release = False
        self._counts = {name: dict(tick=0, input=0, render=0) for name in ('build', 'observe')}
        self._last_level_id = None
        self._last_coords = ()
        self.active = self._create(BuildApp)
        self.renderer = SerialRenderer(self)
        self._compat_compositor = None

    def _create(self, factory):
        return factory(self.width, self.height, root=self.root,
                       font_path=self.font_path, work_path=self.work_path)

    def __getattr__(self, name):
        return getattr(self.active, name)

    @property
    def mode(self):
        return self.active_module

    @property
    def mode_revision(self):
        return self.module_epoch, self.active.mode_revision

    @property
    def inference_context(self):
        return self.module_epoch, self.active_module == 'build'

    @property
    def selected_id(self):
        return self.active.selected_id

    @selected_id.setter
    def selected_id(self, value):
        self.active.selected_id = value

    @property
    def debug(self):
        return self.active.debug

    @debug.setter
    def debug(self, value):
        self.active.debug = value

    @property
    def quit_requested(self):
        return self.active.quit_requested

    @quit_requested.setter
    def quit_requested(self, value):
        self.active.quit_requested = value

    @property
    def metrics(self):
        return dict(self.active.metrics, active_module=self.active_module,
                    module_epoch=self.module_epoch, mode_revision=self.mode_revision,
                    module_counts={name: dict(counts) for name, counts in self._counts.items()},
                    inactive_module_loaded=False, active_ui_modules=1,
                    entry_pinch_release_required=self._require_pinch_release,
                    transition_perf=self.transition_perf,
                    transition_monotonic=self.transition_monotonic)

    def _release_active(self):
        child = self.active
        child.cancel_interaction()
        renderer = child.renderer
        renderer.release_inactive_scratch()
        for name in ('_canvas', '_packet', '_key', 'prepared_scene', '_snapshot_source',
                     '_controls_canvas', '_controls_mask', '_controls_key', '_controls_template'):
            if hasattr(renderer, name):
                setattr(renderer, name, None)
        renderer.owner = None
        for name in ('scene', 'displayed_scene', 'pinch_scene'):
            if hasattr(child.spatial, name):
                setattr(child.spatial, name, None)
        child.spatial.renderer = None
        child.spatial.projection_stage = None
        child.spatial.ui = None
        child._compat_compositor = None
        self.active = None

    def _transition(self, module):
        self._release_active()
        self.active_module = module
        self.module_epoch += 1
        self.transition_perf = time.perf_counter()
        self.transition_monotonic = time.monotonic()
        self._require_pinch_release = True
        self._compat_compositor = None

    def _enter_observe(self):
        self._last_level_id = self.active.spatial.level.id
        self._last_coords = tuple(self.active.spatial.board.coords)
        self._transition('observe')
        self.active = self._create(ObserveApp)
        self.active.load_construction(self._last_level_id, self._last_coords)

    def _enter_build(self):
        self._transition('build')
        self.active = self._create(BuildApp)
        self.active.spatial.selected_level_id = self._last_level_id
        self.active.selected_entry = next(index+1 for index, level in enumerate(LEVELS)
                                          if level.id == self._last_level_id)

    def _call(self, method, *args, count='input', **kwargs):
        child = self.active
        previous_check = child.spatial.check_result
        self._counts[self.active_module][count] += 1
        result = getattr(child, method)(*args, **kwargs)
        checked = child.spatial.check_result
        if self.active_module == 'build':
            if checked is not previous_check and checked is not None and checked.passed:
                self._enter_observe()
        elif child.stage == 'levels':
            self._enter_build()
        return result

    def accepts_inference(self, result):
        captured = getattr(result, 'captured_at', None)
        return (getattr(result, 'mode_epoch', None) == self.module_epoch
                and isinstance(captured, (int, float)) and math.isfinite(captured)
                and captured >= self.transition_perf)

    def accepts_voice(self, payload):
        return (valid_voice_payload(payload)
                and payload['utterance_ended_at'] >= self.transition_monotonic)

    def _allow_observation(self, detections):
        if not self._require_pinch_release:
            return True
        if self.active.controls._points(detections, 'pinch_point'):
            return False
        self._require_pinch_release = False
        self.active.controls.reset(require_release=False)
        return True

    def observe_inference(self, result, now):
        if not self.accepts_inference(result) or not self._allow_observation(result.detections):
            return []
        return self._call('observe_inference', result, now)

    def observe_sequenced(self, detections, now, sequence):
        if not self._allow_observation(detections):
            return []
        if callable(getattr(self.active, 'observe_sequenced', None)):
            return self._call('observe_sequenced', detections, now, sequence)
        return self._call('observe', detections, now)

    def observe(self, detections, now):
        detections = tuple(detections)
        if not self._allow_observation(detections):
            return []
        return self._call('observe', detections, now)

    def tick(self, now):
        return self._call('tick', now, count='tick')

    def hit_test(self, point):
        return self.active.hit_test(point)

    def handle_events(self, events, now):
        # A transition ends this batch; later events were formed against old UI.
        epoch = self.module_epoch
        for event in events:
            self._call('handle_events', (event,), now)
            if epoch != self.module_epoch:
                break

    def _button(self, name, now):
        return self._call('_button', name, now)

    def choose_level(self, level_id):
        if self.active_module != 'build':
            return False
        return self._call('choose_level', level_id)

    def return_to_levels(self):
        if self.active_module == 'observe':
            self._enter_build()
            return True
        return self._call('return_to_levels')

    def go_back(self):
        if self.active_module == 'observe':
            return self.return_to_levels()
        return self._call('go_back')

    def _switch(self, stage):
        if stage == 'levels':
            return self.return_to_levels()
        return self._call('_switch', stage)

    def cancel_interaction(self, now=None):
        return self.active.cancel_interaction(now)

    def apply_voice_command(self, label, now=None):
        return self._call('apply_voice_command', label, now)

    def set_displayed_scene(self, scene, revision, presented_at, mode_revision=None, pose=None):
        if self.active_module != 'observe' or mode_revision != self.mode_revision:
            return False
        return self.active.set_displayed_scene(scene, revision, presented_at,
                                               mode_revision[1], pose)

    def can_observe_without_display(self, detections):
        if self._require_pinch_release:
            return not self.active.controls._points(detections, 'pinch_point')
        method = getattr(self.active, 'can_observe_without_display', None)
        return bool(method(detections)) if callable(method) else False

    def pause_unpresented_input(self, detections, now):
        method = getattr(self.active, 'pause_unpresented_input', None)
        return method(detections, now) if callable(method) else False

    def render(self, bgr, now=None):
        from town_creator.layers import DisplayCompositor
        now = time.perf_counter() if now is None else float(now)
        self.tick(now)
        packet = self.renderer.prepare(self, now, sum(item['render'] for item in self._counts.values())+1)
        if self._compat_compositor is None:
            self._compat_compositor = DisplayCompositor(self.width, self.height, 'tint')
        output = bgr.copy()
        self._compat_compositor.compose_into(output, packet)
        self.active._skip_post_prepare_animation_once = False
        return output


FourLevelsApp = SerialApp
