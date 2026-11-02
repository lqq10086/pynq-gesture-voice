"""Gesture windows rendered independently of the detector worker."""
from __future__ import annotations

import math
import threading
import time
from collections import deque

from .core import Detection, InteractionState
from .games import GameHub
from .spatial_ui import configure_board_blur
from .zoom_background import ZoomBackground


class AsyncGestureApplication:
    """Accept mirrored-space detections and render current camera frames.

    The worker submits completed results. Only the display thread
    touches game state. Repainting or resubmitting the same result never adds
    a second gesture observation.
    """

    ZOOM_ANIMATION_S = .30

    def __init__(self, mode="home", stale_s=0.9, visual_quality="glass"):
        if mode not in ("home", "menu") + GameHub.modes:
            raise ValueError("Unknown application mode")
        if not math.isfinite(stale_s) or stale_s <= 0:
            raise ValueError("stale_s must be positive and finite")
        configure_board_blur(quality=visual_quality)
        self.hub = GameHub(mode)
        self.state = InteractionState()
        self.stale_s = float(stale_s)
        self.detections = []
        self.last_result_id = -1
        self.source_monotonic = None
        self.completed_monotonic = None
        self.fps = 0.0
        self.timings = {}
        self._lock = threading.Lock()
        self._pending = []
        self._pending_drops = 0
        self.dropped_results = 0
        self._submitted_result_id = -1
        self._last_observed_source = None
        self._last_observed_completed = None
        self._expired = True
        self._last_time = None
        self._shape = None
        self.display_zoom = 1.0
        self._zoom_animation_started = None
        self._zoom_animation_from = 1.0
        self._zoom_animation_target = 1.0
        self._zoom_background = ZoomBackground()
        self._trace = None
        self._trace_context = None
        self._trace_duplicate_id = -1
        self.trace_errors = 0
        self._ui_thread = threading.get_ident()
        self._voice_event_ids = deque(maxlen=128)
        self._gesture_not_before = None
        self.voice_status = dict(state="disabled", listening=False)

    def _assert_ui_thread(self):
        if threading.get_ident() != self._ui_thread:
            raise RuntimeError("Application events must be consumed on the UI thread")

    def set_voice_status(self, status=None, **fields):
        """Publish a controller snapshot from the display thread for the HUD."""
        self._assert_ui_thread()
        if status is not None:
            self.voice_status.update(status)
        self.voice_status.update(fields)
        self.hub.voice_status = self.voice_status

    def pop_voice_request(self):
        """Return one UI session request; the controller owns all audio work."""
        self._assert_ui_thread()
        request, self.hub._voice_request = self.hub._voice_request, None
        return request

    def consume_voice_command(self, command, now=None, event_id=None,
                              created_monotonic=None, ttl_s=1.5):
        """Consume a fixed keyword once on the UI thread.

        created_monotonic is the completed recognition/event publication time,
        not the start of the 1.5 second audio analysis window.
        """
        self._assert_ui_thread()
        now = time.monotonic() if now is None else float(now)
        created = now if created_monotonic is None else float(created_monotonic)
        mapping = dict(one="tic_tac_toe", two="memory_match",
                       three="palm_bounce", four="block_builder")
        result = dict(accepted=False, command=command, action=None,
                      mode=self.hub.mode, reason=None)
        if command not in (*mapping, "on", "back", "off", "stop"):
            result['reason'] = 'unknown_command'
        elif (not all(math.isfinite(value) for value in (now, created, ttl_s))
              or ttl_s <= 0 or created > now):
            result['reason'] = 'invalid_timestamp'
        elif now - created > ttl_s:
            result['reason'] = 'stale_event'
        elif event_id is not None and event_id in self._voice_event_ids:
            result['reason'] = 'duplicate_event'
        if result['reason'] is not None:
            return result
        if event_id is not None:
            self._voice_event_ids.append(event_id)
        target = None
        if command in mapping:
            target, action = mapping[command], 'open_app'
        elif command == 'on':
            target, action = 'menu', 'open_menu'
        elif command == 'back':
            target = 'home' if self.hub.mode in ('home', 'menu') else 'menu'
            action = 'back'
        elif command == 'off':
            target = 'menu' if self.hub.mode in GameHub.modes else self.hub.mode
            action = 'close_app'
        else:
            action = 'stop_listening'
        if target is not None:
            self.hub.navigate(target)
            self.state.reset()
            self.display_zoom = 1.0
            self._cancel_zoom_animation()
            self._expire()
            self._gesture_not_before = now
            with self._lock:
                self._pending = []
                self._pending_drops = 0
        result.update(accepted=True, action=action, mode=self.hub.mode)
        self.set_voice_status(last_command=command, result=action)
        self._emit(dict(kind='voice_action', monotonic=now, event_id=event_id,
                        event_wait_ms=(now - created) * 1000, **result))
        return result

    def set_trace(self, callback):
        """Attach a nonblocking event sink, or None to disable observation."""
        self._trace = callback
        self._trace_context = None
        self._trace_duplicate_id = -1
        self.hub.set_trace(self._emit if callback is not None else None)

    def _emit(self, event):
        if self._trace is not None:
            payload = dict(self._trace_context or ())
            payload.update(event)
            payload.setdefault('application', self.hub.mode)
            try:
                self._trace(payload)
            except Exception:
                # Diagnostic failures must not change hardware or UI decisions.
                self.trace_errors += 1

    def submit_detections(self, detections: list[Detection], result_id: int,
                          source_monotonic: float, completed_monotonic: float,
                          source_image=None, frame_id=None):
        """Publish one result; source_image, if given, is its immutable mirrored frame."""
        with self._lock:
            if result_id <= self._submitted_result_id:
                if self._trace is not None and result_id > self._trace_duplicate_id:
                    self._trace_duplicate_id = result_id
                    self._emit(dict(kind='app_refusal', reason=('already_consumed' if
                                    result_id <= self.last_result_id else 'duplicate_submission'),
                                    result_id=result_id, frame_id=frame_id,
                                    source_monotonic=source_monotonic,
                                    completed_monotonic=completed_monotonic,
                                    consume_monotonic=None, monotonic=time.monotonic()))
                return
            self._submitted_result_id = result_id
            if self._pending:
                self._pending_drops += 1
                self.dropped_results += 1
                if self._trace is not None:
                    _, old_id, old_source, old_completed, _, old_frame = self._pending[-1]
                    self._emit(dict(kind='app_refusal', reason='superseded_result',
                                    result_id=old_id, frame_id=old_frame,
                                    source_monotonic=old_source,
                                    completed_monotonic=old_completed,
                                    consume_monotonic=None, monotonic=time.monotonic(),
                                    replacement_result_id=result_id))
            self._pending[:] = [(list(detections), result_id, source_monotonic,
                                 completed_monotonic, source_image, frame_id)]

    def _expire(self, *, preserve_resize=False):
        """Forget an old pointer without manufacturing detector releases."""
        self.detections = []
        self.hub.pointer = None
        self.hub._pinch_points = []
        self.hub._candidate = None
        self.hub._stable = 0
        self.hub._missing = 0
        if not preserve_resize:
            self.hub._resize.reset()
        # Only a fresh no-pinch result releases an existing click latch.
        # Display freshness is shorter than the independent palm session grace.
        # An accepted zoom target outlives its detector boxes for at most the
        # independent animation duration. Expiry creates no new palm action.
        self.state.expire_zoom_result()
        self._expired = True

    def _cancel_zoom_animation(self):
        """Explicit pinch/window actions take priority over background motion."""
        self.state.zoom = self.display_zoom
        self._zoom_animation_started = None
        self._zoom_animation_from = self.display_zoom
        self._zoom_animation_target = self.display_zoom

    def _animate_zoom(self, now):
        if self.state.zoom != self._zoom_animation_target:
            self._zoom_animation_from = self.display_zoom
            self._zoom_animation_target = self.state.zoom
            self._zoom_animation_started = now
        if self._zoom_animation_started is not None:
            progress = max(0., min(1., (now - self._zoom_animation_started) /
                                      self.ZOOM_ANIMATION_S))
            if progress >= 1. - 1e-9:
                self.display_zoom = self._zoom_animation_target
                self._zoom_animation_started = None
            else:
                blend = 1. - (1. - progress) ** 3
                self.display_zoom = self._zoom_animation_from + (
                    self._zoom_animation_target - self._zoom_animation_from) * blend

    def render_frame(self, mirrored_bgr, now=None, inplace=False):
        """Set inplace only for a private frame; the default preserves input."""
        started = time.perf_counter()
        live_clock = now is None
        now = time.monotonic() if now is None else float(now)
        height, width = mirrored_bgr.shape[:2]
        if self._shape is not None and self._shape != (height, width):
            if self._trace is not None:
                self._emit(dict(kind='app_refusal', reason='frame_shape_changed',
                                monotonic=now, shape=[height, width]))
            latched = self.hub._latched
            self.hub = GameHub(self.hub.mode)
            self.hub.voice_status = self.voice_status
            self.hub.set_trace(self._emit if self._trace is not None else None)
            self.hub._latched = latched
            self.state.reset()
            self.display_zoom = 1.0
            self._cancel_zoom_animation()
            self._expire()
            with self._lock:
                if self._trace is not None:
                    for _, result_id, source, completed, _, frame_id in self._pending:
                        self._emit(dict(kind='app_refusal', reason='frame_shape_changed',
                                        monotonic=now, consume_monotonic=None,
                                        frame_id=frame_id, result_id=result_id,
                                        source_monotonic=source, completed_monotonic=completed))
                self._pending = []
                self._pending_drops = 0
        self._shape = (height, width)
        elapsed = None if self._last_time is None else max(0.0, now - self._last_time)
        if elapsed is not None and elapsed >= 0.001:
            instant = min(240.0, 1 / (now - self._last_time))
            self.fps = instant if self.fps == 0 else self.fps * 0.8 + instant * 0.2
        self._last_time = now
        with self._lock:
            pending, self._pending = self._pending, []
            dropped, self._pending_drops = self._pending_drops, 0

        # Lost observations cannot establish continuous pinch/release/resize.
        # Retain an already latched click until real release results arrive.
        if dropped or (not self._expired and now - self.source_monotonic > self.stale_s):
            if self._trace is not None:
                self._emit(dict(kind='app_refusal', reason=('lost_observations' if dropped
                                else 'no_fresh_result'), monotonic=now, dropped_results=dropped))
            self._expire(preserve_resize=not dropped)

        # Detector boxes expire by camera age; a captured window waits for the
        # next result by accepted completion age. Display ticks never move it.
        # Check even after box expiry so an actual inference outage cancels it.
        if self.hub.resizing and (self._last_observed_completed is None or
                                  now - self._last_observed_completed > self.stale_s):
            self.hub._resize.reset()

        self.state.expire_zoom_session(now, trace=(self._emit if self._trace is not None else None))

        consumed_results = []
        fresh_results = 0
        for detections, result_id, source, completed, source_image, frame_id in pending:
            consume_monotonic = time.monotonic() if live_clock else now
            before = dict(mode=self.hub.mode, latched=self.hub._latched,
                          stable=self.hub._stable, missing=self.hub._missing,
                          pointer=self.hub.pointer, candidate=self.hub._candidate)
            clicked = False
            self.last_result_id = result_id
            self.source_monotonic, self.completed_monotonic = source, completed
            fresh_result = (source is not None and completed is not None
                            and math.isfinite(source) and math.isfinite(completed)
                            and source <= completed <= consume_monotonic
                            and 0 <= consume_monotonic - source <= self.stale_s
                            and (self._gesture_not_before is None or
                                 source > self._gesture_not_before)
                            and (self._last_observed_source is None or
                                 source > self._last_observed_source))
            if self._trace is not None:
                self._trace_context = dict(frame_id=frame_id, result_id=result_id,
                    source_monotonic=source, completed_monotonic=completed,
                    consume_monotonic=consume_monotonic, monotonic=consume_monotonic)
                reason = None
                if source is None or completed is None or not math.isfinite(source) or not math.isfinite(completed):
                    reason = 'invalid_timestamp'
                elif not source <= completed <= consume_monotonic or source > consume_monotonic:
                    reason = 'invalid_timestamp_order'
                elif consume_monotonic - source > self.stale_s:
                    reason = 'stale_result'
                elif self._gesture_not_before is not None and source <= self._gesture_not_before:
                    reason = 'before_voice_navigation'
                elif self._last_observed_source is not None and source <= self._last_observed_source:
                    reason = 'repeated_source'
                self._emit(dict(kind='app_consume', fresh=fresh_result, reason=reason,
                    input_size=[width, height], display_zoom=self.display_zoom,
                    coordinate_space='mirrored_unzoomed_input',
                    detections=[dict(label=d.label, confidence=float(d.confidence),
                                     box=list(d.box)) for d in detections]))
            if fresh_result:
                fresh_results += 1
                self.detections = detections
                self._expired = False
                self._last_observed_source = source
                self._last_observed_completed = completed
                interaction = detections
                if self.hub.mode == "zoom" and self.display_zoom != 1:
                    def map_point(point):
                        return (round((point[0] - width / 2) * self.display_zoom + width / 2),
                                round((point[1] - height / 2) * self.display_zoom + height / 2))
                    interaction = [Detection(d.label, d.confidence,
                                             (*map_point(d.box[:2]), *map_point(d.box[2:])))
                                   for d in detections]
                    if self._trace is not None:
                        self._emit(dict(kind='coordinate_transform', transform='background_zoom',
                            coordinate_space='mirrored_display', zoom=self.display_zoom,
                            detections=[dict(label=d.label,
                            confidence=float(d.confidence), box=list(d.box)) for d in interaction]))
                previous = self.hub.mode
                clicked = self.hub.observe(interaction, width, height, image=source_image,
                                           source_now=source, now=consume_monotonic,
                                           trace_context=self._trace_context if self._trace is not None else None)
                if self.hub.mode != previous:
                    self.state.reset()
                    self.display_zoom = 1.0
                    self._cancel_zoom_animation()
                    self._expire()
                elif self.hub.mode == "zoom":
                    if self.hub.resizing or any(d.label == 'pinch_point' for d in detections):
                        if self._trace is not None:
                            self._emit(dict(kind='palm_zoom', action='not_applied',
                                            reason='pinch_or_resize_present'))
                        self.state.end_zoom_session()
                        self._cancel_zoom_animation()
                    else:
                        self.state.update_detections(detections,
                            trace=(self._emit if self._trace is not None else None),
                            now=consume_monotonic)
            else:
                self._expire()
            after = dict(mode=self.hub.mode, latched=self.hub._latched,
                         stable=self.hub._stable, missing=self.hub._missing,
                         pointer=self.hub.pointer, candidate=self.hub._candidate)
            consumed_results.append(dict(
                result_id=result_id, source=source, completed=completed,
                consume_monotonic=consume_monotonic,
                delivery_wait_ms=(None if completed is None else
                                  (consume_monotonic - completed) * 1000),
                fresh=fresh_result, before=before, after=after,
                pinch_present=any(d.label == "pinch_point" for d in detections),
                candidate_started=before['candidate'] is None and after['candidate'] is not None,
                click=bool(clicked), released=before['latched'] and not after['latched'],
                detections=[dict(label=d.label, confidence=float(d.confidence),
                                 box=[float(value) for value in d.box]) for d in detections]))
        if not pending and not self._expired and now - self.source_monotonic > self.stale_s:
            self._expire(preserve_resize=True)

        render_start = time.perf_counter()
        if self.hub.mode == 'block_builder' and self.hub.game is not None:
            self.hub.game.expire_tracking(now)
        if self.hub.mode != "zoom":
            self.state.reset()
            self.display_zoom = 1.0
            self._cancel_zoom_animation()
        else:
            self._animate_zoom(time.monotonic() if live_clock else now)
        if self.display_zoom == 1.0:
            result = mirrored_bgr if inplace else mirrored_bgr.copy()
        else:
            result = self._zoom_background.render(mirrored_bgr, self.display_zoom)
        result = self.hub.render(result, self.fps, self.display_zoom)
        self.hub.draw_pointer(result)
        ended = time.perf_counter()
        result_age_monotonic = consumed_results[-1]['consume_monotonic'] if consumed_results else now
        self.timings = dict(new_result=bool(fresh_results), new_results=fresh_results,
                            dropped_results=dropped, dropped_results_total=self.dropped_results,
                            display_zoom=self.display_zoom,
                            zoom_target=self.state.zoom, zoom_state=self.state.zoom_state,
                            zoom_animation_active=self._zoom_animation_started is not None,
                            consumed_results=consumed_results,
                            result_age_monotonic=result_age_monotonic,
                            result_age_ms=(None if self.source_monotonic is None else
                                           (result_age_monotonic - self.source_monotonic) * 1000),
                            observe_ms=(render_start - started) * 1000,
                            render_ms=(ended - render_start) * 1000,
                            total_ms=(ended - started) * 1000)
        return result
