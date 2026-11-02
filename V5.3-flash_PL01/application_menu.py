"""One four-card selection page for construction displays and drawing town."""
from dataclasses import replace
import math
from pathlib import Path
import time

import cv2
import numpy as np

from combined.flat_ui import FlatUi, opaque_packet
from combined.voice import valid_voice_payload
from spatial_build.app import inside
from spatial_build.controls import GestureController
from town_creator.layers import DisplayCompositor, PreparedUI


class ApplicationRenderer:
    def __init__(self, owner):
        self.owner = owner
        self._canvas = None
        self._key = self._packet = None
        self.preparations = self.cache_hits = 0

    def visual_key(self, app, now):
        if app.active is not None:
            key = app.active.renderer.visual_key(app.active, now)
        else:
            pointer = app.controls.pointer
            key = (None if pointer is None else tuple(map(int, pointer)), app.debug)
        return app.module_epoch, app.active_application, key

    def presentation_guard_key(self, app, now):
        if app.active is None:
            return self.visual_key(app, now)
        child = app.active
        guard = getattr(child.renderer, 'presentation_guard_key', child.renderer.visual_key)
        return app.module_epoch, app.active_application, guard(child, now)

    def prepare(self, app, now, revision):
        if app.active is not None:
            return app.active.renderer.prepare(app.active, now, revision)
        key = self.visual_key(app, now)
        if key == self._key:
            self.cache_hits += 1
            return PreparedUI(int(revision), float(now), app.width, app.height,
                              self._packet.patches, self._packet.glass_regions)
        if self._canvas is None:
            self._canvas = np.zeros((app.height, app.width, 3), np.uint8)
        image, ui = self._canvas, app.ui
        image.fill(0)
        ui.button(image, app.layout['close'], 'close')
        for number in range(1, 5):
            name, label = 'display_%s' % number, '展示%s' % number
            rect = app.layout[name]
            ui.panel(image, rect)
            ui.text(image, label, ((rect[0]+rect[2])//2,
                                  (rect[1]+rect[3])//2-int(app.height*.025)),
                    size=int(app.height*.045), align='center')
        if app.controls.pointer is not None:
            point = tuple(map(int, app.controls.pointer))
            cv2.circle(image, point, max(3, int(app.height*.006)),
                       (204, 210, 212), 1, cv2.LINE_AA)
        self._packet = opaque_packet(image, revision, now)
        self._key = key
        self.preparations += 1
        return self._packet

    @property
    def prepared_scene(self):
        child = self.owner.active
        return getattr(child.renderer, 'prepared_scene', None) if child is not None else None

    def release_inactive_scratch(self):
        self._canvas = self._packet = self._key = None
        child = self.owner.active
        release = getattr(getattr(child, 'renderer', None), 'release_inactive_scratch', None)
        if callable(release):
            release()

    def metrics(self):
        child = self.owner.active
        return dict(active_application=self.owner.active_application,
                    application_epoch=self.owner.module_epoch,
                    menu_preparations=self.preparations, menu_cache_hits=self.cache_hits,
                    camera_free=True,
                    child=child.renderer.metrics() if child is not None else None)


class ApplicationMenu:
    def __init__(self, width=1280, height=720, root=None, font_path=None, work_path=None):
        self.width, self.height = int(width), int(height)
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent
        self.font_path, self.work_path = font_path, work_path
        self.active = None
        self.active_application = 'menu'
        self.selected_entry = 1
        self.module_epoch = 0
        self.transition_perf = time.perf_counter()
        self.transition_monotonic = time.monotonic()
        self._require_pinch_release = False
        self._last_sequence = None
        self._child_context = None
        self._menu_controls = GestureController()
        self._menu_debug = self._quit_requested = False
        self._compat_compositor = None
        menu_font = font_path or Path(__file__).resolve().parent/'living_town/assets/menu_font.ttf'
        self.ui = FlatUi(self.width, self.height, font_path=menu_font)
        self.layout = {name: tuple(int(v*(self.width if i%2 == 0 else self.height))
                                  for i, v in enumerate(rect))
                       for name, rect in dict(close=(.474,.055,.526,.138),
                                              display_1=(.10,.23,.47,.49),
                                              display_2=(.53,.23,.90,.49),
                                              display_3=(.10,.57,.47,.83),
                                              display_4=(.53,.57,.90,.83)).items()}
        self.renderer = ApplicationRenderer(self)

    def __getattr__(self, name):
        child = self.__dict__.get('active')
        if child is None:
            raise AttributeError(name)
        return getattr(child, name)

    @property
    def stage(self):
        return self.active.stage if self.active is not None else 'menu'

    @property
    def active_module(self):
        return getattr(self.active, 'active_module', self.active_application)

    @property
    def mode_revision(self):
        return self.module_epoch, getattr(self.active, 'mode_revision', self.stage)

    @property
    def inference_context(self):
        context = getattr(self.active, 'inference_context', (0, False))
        return self.module_epoch, bool(context[1])

    @property
    def controls(self):
        return self.active.controls if self.active is not None else self._menu_controls

    @property
    def debug(self):
        return self.active.debug if self.active is not None else self._menu_debug

    @debug.setter
    def debug(self, value):
        if self.active is None:
            self._menu_debug = bool(value)
        else:
            self.active.debug = bool(value)

    @property
    def selected_id(self):
        return getattr(self.active, 'selected_id', None)

    @selected_id.setter
    def selected_id(self, value):
        if self.active is not None:
            self.active.selected_id = value

    @property
    def quit_requested(self):
        return self._quit_requested

    @quit_requested.setter
    def quit_requested(self, value):
        self._quit_requested = bool(value)

    @property
    def metrics(self):
        child = self.active
        metrics = getattr(child, 'metrics', {})
        return dict(active_application=self.active_application, active_module=self.active_module,
                    module_epoch=self.module_epoch, mode_revision=self.mode_revision,
                    stage=self.stage, active_ui_modules=int(child is not None),
                    inactive_module_loaded=False,
                    entry_pinch_release_required=self._require_pinch_release,
                    transition_perf=self.transition_perf,
                    transition_monotonic=self.transition_monotonic,
                    child=metrics() if callable(metrics) else metrics)

    def _context(self):
        return (getattr(self.active, 'module_epoch', None), self.stage,
                getattr(self.active, 'mode_revision', None))

    def _boundary(self):
        self.module_epoch += 1
        self.transition_perf = time.perf_counter()
        self.transition_monotonic = time.monotonic()
        self._require_pinch_release = True
        self._child_context = self._context()

    def _enter(self, name):
        if name not in ('display_1', 'display_2', 'display_3', 'display_4') or self.active is not None:
            return False
        spatial = name != 'display_4'
        if spatial:
            from combined.serial_app import SerialApp as factory
            from spatial_build.model import LEVELS
        else:
            from living_town import create_app as factory
        child = factory(self.width, self.height, root=self.root,
                        font_path=self.font_path, work_path=self.work_path)
        if spatial:
            self.selected_entry = int(name[-1])
            child.choose_level(LEVELS[self.selected_entry-1].id)
        self.cancel_interaction()
        self.active, self.active_application = child, 'spatial' if spatial else 'town'
        self._boundary()
        self.renderer.release_inactive_scratch()
        return True

    def return_to_menu(self):
        if self.active is None:
            return False
        self.active.cancel_interaction()
        self.renderer.release_inactive_scratch()
        self.active, self.active_application = None, 'menu'
        self._menu_controls.reset(require_release=True)
        self._boundary()
        return True

    def _call(self, method, *args, **kwargs):
        child = self.active
        result = getattr(child, method)(*args, **kwargs)
        if child.quit_requested or (self.active_application == 'spatial' and child.stage == 'levels'):
            self.return_to_menu()
        elif self._context() != self._child_context:
            self._boundary()
        return result

    def accepts_inference(self, result):
        captured = getattr(result, 'captured_at', None)
        return (getattr(result, 'mode_epoch', None) == self.module_epoch
                and type(captured) in (int, float) and math.isfinite(captured)
                and captured >= self.transition_perf)

    def accepts_voice(self, payload):
        return (valid_voice_payload(payload)
                and payload['utterance_ended_at'] >= self.transition_monotonic)

    def _allow_observation(self, detections):
        if not self._require_pinch_release:
            return True
        if self.controls._points(detections, 'pinch_point'):
            return False
        self._require_pinch_release = False
        self.controls.reset(require_release=False)
        return True

    def _child_result(self, result):
        epoch = getattr(self.active, 'module_epoch', result.mode_epoch)
        def valid(sample):
            return (sample.mode_epoch == self.module_epoch and math.isfinite(sample.captured_at)
                    and sample.captured_at >= self.transition_perf)
        observations = result.placement_observations
        observations = (tuple(replace(sample, mode_epoch=epoch) for sample in observations)
                        if all(valid(sample) for sample in observations) else ())
        cycle = result.placement_cycle
        if cycle is not None:
            if (cycle.mode_epoch != self.module_epoch or not math.isfinite(cycle.released_at)
                    or cycle.released_at < self.transition_perf or not all(valid(s) for s in cycle.pinches)):
                cycle = None
            else:
                cycle = replace(cycle, mode_epoch=epoch,
                                pinches=tuple(replace(s, mode_epoch=epoch) for s in cycle.pinches))
        return replace(result, mode_epoch=epoch, placement_observations=observations, placement_cycle=cycle)

    def observe_inference(self, result, now):
        if (not self.accepts_inference(result)
                or (self._last_sequence is not None and result.sequence <= self._last_sequence)):
            return []
        self._last_sequence = result.sequence
        if not self._allow_observation(result.detections):
            return []
        if self.active is None:
            return self.observe(result.detections, now)
        if callable(getattr(self.active, 'observe_inference', None)):
            return self._call('observe_inference', self._child_result(result), now)
        return self.observe_sequenced(result.detections, now, result.sequence)

    def observe_sequenced(self, detections, now, sequence):
        detections = tuple(detections)
        if not self._allow_observation(detections):
            return []
        if self.active is not None and callable(getattr(self.active, 'observe_sequenced', None)):
            return self._call('observe_sequenced', detections, now, sequence)
        return self.observe(detections, now)

    def observe(self, detections, now):
        detections = tuple(detections)
        if not self._allow_observation(detections):
            return []
        if self.active is not None:
            return self._call('observe', detections, now)
        events = self.controls.observe(detections, self.width, self.height, now, self.hit_test)
        self.handle_events(events, now)
        return events

    def tick(self, now):
        if self.active is not None:
            return self._call('tick', now)
        events = self.controls.tick(now)
        self.handle_events(events, now)
        return events

    def hit_test(self, point):
        if self.active is not None:
            return self.active.hit_test(point)
        for name, rect in self.layout.items():
            if inside(rect, point):
                return 'button', name
        return 'empty', None

    def handle_events(self, events, now):
        epoch = self.module_epoch
        for event in events:
            if self.active is not None:
                self._call('handle_events', (event,), now)
            elif event.get('kind') == 'click' and event.get('intent') == 'button':
                self._button(event.get('target'), now)
            if epoch != self.module_epoch:
                break

    def _button(self, name, now):
        if self.active is not None:
            return self._call('_button', name, now)
        if name == 'close':
            self.quit_requested = True
            return True
        return self._enter(name)

    def go_back(self):
        if self.active is None:
            return False
        result = self._call('go_back')
        return result or self.return_to_menu()

    def cancel_interaction(self, now=None):
        if self.active is not None:
            return self.active.cancel_interaction(now)
        self.controls.reset(require_release=True)

    def apply_voice_command(self, label, now=None):
        if self.active_application == 'town' and label == 'off':
            return self.return_to_menu()
        if self.active is None:
            choices = {'one': 'display_1', 'two': 'display_2', 'three': 'display_3',
                       'four': 'display_4'}
            if label == 'on':
                return self._enter('display_%s' % self.selected_entry)
            if label in choices:
                return self._enter(choices[label])
            return False
        return self._call('apply_voice_command', label, now)

    def set_displayed_scene(self, scene, revision, presented_at, mode_revision=None, pose=None):
        if self.active is None or mode_revision != self.mode_revision:
            return False
        method = getattr(self.active, 'set_displayed_scene', None)
        return (method(scene, revision, presented_at, mode_revision[1], pose)
                if callable(method) else False)

    def can_observe_without_display(self, detections):
        if self._require_pinch_release:
            return not self.controls._points(detections, 'pinch_point')
        method = getattr(self.active, 'can_observe_without_display', None)
        return bool(method(detections)) if callable(method) else False

    def pause_unpresented_input(self, detections, now):
        method = getattr(self.active, 'pause_unpresented_input', None)
        return method(detections, now) if callable(method) else False

    def render(self, bgr, now=None):
        now = time.perf_counter() if now is None else float(now)
        self.tick(now)
        packet = self.renderer.prepare(self, now, self.renderer.preparations+1)
        if self._compat_compositor is None:
            self._compat_compositor = DisplayCompositor(self.width, self.height, 'tint')
        output = bgr.copy()
        self._compat_compositor.compose_into(output, packet)
        return output


create_app = ApplicationMenu
