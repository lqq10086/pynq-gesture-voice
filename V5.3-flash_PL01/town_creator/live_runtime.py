"""Three owners: immutable capture, unchanged AI, and camera-free UI preparation.

The display thread only polls completed data. Neither model initialization nor
heavy UI preparation runs on that thread. Mailboxes retain one newest item.
"""
from collections import deque
from dataclasses import replace
import copy
import math
from pathlib import Path
import threading
import time

import cv2
import numpy as np

from gesture_app.inference import NAMES_2, create_runner, decode, preprocess
from spatial_build.runtime import (AsyncGestureWorker, DeferredFrame, InferenceResult,
                                  PlacementCycle, PlacementObservation, _stats)


def _detection_sample(detections):
    result = []
    for detection in detections[:8]:
        get = detection.get if isinstance(detection, dict) else lambda key, default=None: getattr(detection,key,default)
        box = get('box')
        center = get('center')
        result.append(dict(label=get('label'),confidence=float(get('confidence',0.)),
                           box=list(map(float,box)) if box is not None else None,
                           center=list(map(float,center)) if center is not None else None))
    return result


class _SerialCameraRunner:
    """Keep raw camera inference under the existing utterance NN mutex."""
    def __init__(self,runner,gate):self.runner,self.gate=runner,gate
    def infer(self,tensor):
        with self.gate.lock:return self.runner.infer(tensor)
    def infer_raw565(self,raw):
        with self.gate.lock:return self.runner.infer_raw565(raw)
    def close(self):self.runner.close()


class _LazyRunner:
    """Create the existing backend on the inference thread, not the caller."""
    def __init__(self, config, root):
        self.config, self.root, self.runner = dict(config), root, None

    def infer(self, tensor):
        if self.runner is None:
            self.runner, names = create_runner(self.config, self.root)
            if tuple(names) != NAMES_2:
                raise ValueError('Expected the two-class gesture model: open_palm, pinch_point')
        return self.runner.infer(tensor)

    def close(self):
        if self.runner is not None:
            self.runner.close()


class CameraGestureWorker(AsyncGestureWorker):
    """Reuse the stable model pipeline; mirror raw camera data on its AI thread."""
    def __init__(self, config=None, root=None, runner=None, inference_gate=None, **kwargs):
        self._owned_offers = self._copied_offers = self._mirrored_frames = 0
        self._camera_source = None
        self._camera_source_mirrored = False
        self._last_camera_source_sequence = self._skipped_camera_source_frames = 0
        self._native_raw_frames = 0
        self._detection_samples = deque(maxlen=128)
        self._detection_counts = dict(empty=0,open_palm=0,pinch_point=0,mixed=0)
        self._placement_pinches = deque(maxlen=2)
        self._placement_cycle = None
        if runner is None:
            if config is None:
                raise ValueError('An existing inference config or runner is required')
            runner = _LazyRunner(config, root or Path.cwd())
        if inference_gate is not None:
            if callable(getattr(runner,'infer_raw565',None)):
                runner = _SerialCameraRunner(runner,inference_gate)
            else:
                from voice_control.session import SerialGestureRunner
                runner = SerialGestureRunner(runner, inference_gate)
        if config is not None:
            kwargs.setdefault('names', tuple(config.get('names', NAMES_2)))
        super().__init__(config=config, root=root, runner=runner, **kwargs)

    def offer(self, mirrored_bgr, captured_at=None):
        return self.offer_camera(mirrored_bgr, captured_at=captured_at, already_mirrored=True)

    def attach_camera_source(self, source, already_mirrored=False):
        """Optionally pull immutable snapshots independently of the display loop.

        The source must provide the existing LatestFrameSource.snapshot API and
        private, C-contiguous snapshot storage. Its owner retains responsibility
        for source.close(). No camera read or waiting driver call occurs here.
        """
        if source is not None and not callable(getattr(source,'snapshot',None)):
            raise TypeError('camera source must implement snapshot(last_sequence)')
        with self._condition:
            self._camera_source = source
            self._camera_source_mirrored = bool(already_mirrored)
            self._last_camera_source_sequence = 0
            self._condition.notify_all()

    def offer_camera(self, frame, captured_at=None, source_sequence=None,
                     already_mirrored=False, owned=False):
        if (not isinstance(frame, np.ndarray) or frame.dtype != np.uint8
                or frame.ndim != 3 or frame.shape[2] != 3 or min(frame.shape[:2]) <= 0):
            raise ValueError('Expected a nonempty uint8 HWC BGR frame')
        captured_at = time.perf_counter() if captured_at is None else float(captured_at)
        if not math.isfinite(captured_at):
            raise ValueError('captured_at must be finite')
        if source_sequence is not None and (type(source_sequence) is not int or source_sequence < 0):
            raise ValueError('source_sequence must be a nonnegative integer')
        if owned:
            if frame.flags.writeable or not frame.flags.c_contiguous:
                raise ValueError('Owned camera frames must be immutable and C-contiguous')
            image = frame
        else:
            image = frame.copy(order='C')
        with self._condition:
            if self._stop or self._finished:
                return None
            self._offered += 1
            if self._pending is not None:
                self._replaced += 1
            if owned: self._owned_offers += 1
            else: self._copied_offers += 1
            source_sequence = self._offered if source_sequence is None else source_sequence
            self._pending = (self._offered, source_sequence, image, captured_at, bool(already_mirrored))
            self._condition.notify()
            return self._offered

    def metrics(self):
        result = super().metrics()
        with self._condition:
            result.update(owned_camera_offers=self._owned_offers,
                          copied_camera_offers=self._copied_offers,
                          mirrored_inference_frames=self._mirrored_frames,
                          native_raw_inference_frames=self._native_raw_frames,
                          camera_source_attached=self._camera_source is not None,
                          last_camera_source_sequence=self._last_camera_source_sequence,
                          skipped_camera_source_frames=self._skipped_camera_source_frames,
                          model_initialization='deferred to inference thread')
            result.update(raw_detection_frames=dict(self._detection_counts),
                          recent_raw_detections=list(self._detection_samples))
        return result

    snapshot_metrics = metrics

    def _run(self):
        # The preprocessing, decoding, timing and failure behavior below match
        # AsyncGestureWorker. Only the input handoff and one-time mirror differ.
        try:
            while True:
                with self._condition:
                    while not self._stop:
                        attached = self._camera_source is not None
                        if self._pending is None and not attached:
                            self._condition.wait()
                            continue
                        remaining = (0. if self._last_started_at is None else
                                     self._last_started_at+self.interval_ms/1000.-time.perf_counter())
                        if remaining > 0:
                            self._condition.wait(min(.01,remaining) if attached else remaining)
                            continue
                        if attached:
                            # snapshot is a short locked pointer exchange, not a
                            # sensor read. Select after the interval so a slow
                            # model never begins work on an old queued frame.
                            try:
                                snapshot = self._camera_source.snapshot(self._last_camera_source_sequence)
                            except Exception as exc:
                                self._error = '%s: %s' % (type(exc).__name__,exc)
                                self._stop = True
                                break
                            if snapshot is None:
                                self._condition.wait(.01)
                                continue
                            image = snapshot.deferred if getattr(snapshot,'deferred',None) is not None else snapshot.image
                            if isinstance(image,DeferredFrame):
                                if self._camera_source_mirrored:
                                    self._error='ValueError: raw565 input must be before the PL display mirror'
                                    self._stop=True
                                    break
                            elif (not isinstance(image,np.ndarray) or image.dtype != np.uint8
                                    or image.ndim != 3 or image.shape[2] != 3
                                    or min(image.shape[:2]) <= 0 or not image.flags.c_contiguous):
                                self._error = 'ValueError: camera snapshot must own a nonempty contiguous BGR frame'
                                self._stop = True
                                break
                            # LatestFrameSource owns a private copy per capture;
                            # both display and inference retain this immutable
                            # reference without additional boundary copies.
                            if not isinstance(image,DeferredFrame):image.setflags(write=False)
                            self._skipped_camera_source_frames += max(0,snapshot.sequence-self._last_camera_source_sequence-1)
                            self._last_camera_source_sequence = snapshot.sequence
                            self._offered += 1
                            self._owned_offers += 1
                            self._pending = (self._offered,snapshot.sequence,image,
                                             snapshot.captured_at,self._camera_source_mirrored)
                        _, source_sequence, image, captured_at, mirrored = self._pending
                        mode_epoch, collect_placement = self._mode_epoch,self._collect_placement
                        context_revision = self._context_revision
                        self._pending = None
                        started_at = time.perf_counter()
                        if self._last_started_at is not None:
                            self._intervals.append((started_at-self._last_started_at)*1000.)
                        self._last_started_at = started_at
                        self._busy = True
                        break
                    if self._stop: break
                inference_ms, detections, error = 0., (), None
                try:
                    if isinstance(image,DeferredFrame):
                        inference_started=time.perf_counter()
                        raw,geometry=self.runner.infer_raw565(image.raw)
                        with self._condition:self._native_raw_frames+=1
                    else:
                        inference_image = image if mirrored else cv2.flip(image, 1)
                        if not mirrored:
                            with self._condition: self._mirrored_frames += 1
                        tensor, geometry = preprocess(inference_image)
                        inference_started = time.perf_counter()
                        raw = self.runner.infer(tensor)
                    inference_ms = (time.perf_counter()-inference_started)*1000.
                    detections = tuple(decode(raw, geometry, names=self.names, **self.postprocess))
                except Exception as exc:
                    error = '%s: %s' % (type(exc).__name__, exc)
                finished_at = time.perf_counter()
                pipeline_ms = (finished_at-started_at)*1000.
                with self._condition:
                    self._busy = False
                    self._processed += 1
                    cycle, observations = None, ()
                    if collect_placement and context_revision == self._context_revision:
                        if error is None and any(d.label == 'pinch_point' for d in detections):
                            self._placement_pinches.append(PlacementObservation(
                                self._processed,captured_at,detections,mode_epoch))
                        else:
                            if error is None and len(self._placement_pinches) == 2:
                                self._placement_cycle = PlacementCycle(tuple(self._placement_pinches),
                                    self._processed,captured_at,mode_epoch)
                            self._placement_pinches.clear()
                        cycle, observations = self._placement_cycle,tuple(self._placement_pinches)
                    self._latest = InferenceResult(self._processed, detections, tuple(image.shape),
                        captured_at, finished_at, inference_ms, error, source_sequence, started_at, pipeline_ms,
                        cycle,observations,mode_epoch)
                    labels = {sample['label'] for sample in _detection_sample(detections)}
                    self._detection_counts['empty'] += not labels
                    self._detection_counts['mixed'] += 'open_palm' in labels and 'pinch_point' in labels
                    for label in ('open_palm','pinch_point'):
                        self._detection_counts[label] += label in labels
                    self._detection_samples.append(dict(sequence=self._processed,source_sequence=source_sequence,
                        captured_at=captured_at,started_at=started_at,finished_at=finished_at,
                        error=error,mode_epoch=mode_epoch,detections=_detection_sample(detections)))
                    self._network_times.append(inference_ms)
                    self._pipeline_times.append(pipeline_ms)
                    if error is None:
                        self._successful += 1
                        if self._first_result_at is None: self._first_result_at = finished_at
                        self._last_result_at = finished_at
                    else:
                        self._error, self._pending, self._stop = error, None, True
        finally:
            try:
                self.runner.close()
            except Exception as exc:
                with self._condition: self._close_error = '%s: %s' % (type(exc).__name__, exc)
            with self._condition:
                self._busy = False
                self._finished = True
                self._condition.notify_all()


class UIWorker:
    """Single-owner interaction + retained UI; preparation never sees a camera.

    Call acknowledge only AFTER submitting that PreparedUI revision to display.
    New detections cannot act on UI which has not yet been displayed. No cached
    inference result is used as a second observation. Buttons/fixtures are
    explicit commands, not synthetic camera detections.
    """
    def __init__(self, width, height, root, config, work_path=None,
                 app_factory=None, prepare_delay=0):
        if int(width) <= 0 or int(height) <= 0:
            raise ValueError('positive output dimensions are required')
        prepare_delay = float(prepare_delay)
        if not math.isfinite(prepare_delay) or prepare_delay < 0:
            raise ValueError('prepare_delay must be finite and nonnegative')
        self.width, self.height = int(width), int(height)
        self.root, self.config, self.work_path = Path(root), dict(config), work_path
        self._app_factory, self._prepare_delay = app_factory, prepare_delay
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._commands = deque()
        self._pending = None
        self._inference_worker = None
        self._context_worker = None
        self._interaction_context = None
        self._current_epoch = 0
        self._last_attached_sequence = 0
        self._highest_offered_sequence = self._last_result_sequence = 0
        self._manual_sequence = 0
        self._latest = None
        self._published_revision = self._displayed_revision = 0
        self._displayed_at = None
        self._geometry_applied_revision = 0
        self._prepared_scenes = {}
        self._accepted = self._stale = self._replaced = self._duplicates = 0
        self._command_drops = self._preparations = self._ticks = 0
        self._epoch_command_drops = self._voice_rejections = self._metadata_drops = 0
        self._preparation_attempts = self._discarded_preparations = 0
        self._prepare_times = deque(maxlen=512)
        self._observation_times = deque(maxlen=512)
        self._observation_samples = deque(maxlen=128)
        self._error = self._command_error = None
        self._ready = self._preparing = self._closed = False
        self._status = dict(stage=None, quit_requested=False, app={})
        self._thread = threading.Thread(target=self._run, name='town-ui-owner', daemon=True)
        self._thread.start()

    def attach_inference(self, worker):
        """Consume completed AI publications without relying on display calls."""
        if worker is not None and not callable(getattr(worker,'poll',None)):
            raise TypeError('inference worker must implement poll(last_sequence)')
        with self._condition:
            self._inference_worker = worker
            self._last_attached_sequence = 0
            self._condition.notify_all()

    def _sync_interaction_context(self, app):
        context = getattr(app,'inference_context',(getattr(app,'module_epoch',0),True))
        with self._condition:
            attached = self._inference_worker
            changed = self._interaction_context is not None and context != self._interaction_context
            synchronize = context != self._interaction_context or attached is not self._context_worker
            self._interaction_context, self._current_epoch = context,context[0]
            self._context_worker = attached
            if changed:
                self._pending = None
                self._prepared_scenes.clear()
                self._ready = False
            self._condition.notify_all()
        # The inference owner acquires its own condition; never nest that lock.
        setter = getattr(attached,'set_interaction_context',None)
        if synchronize and callable(setter): setter(*context)
        return changed

    @staticmethod
    def _uses_view_pipeline(app):
        return (getattr(app,'active_module',None) == 'observe'
                or (not hasattr(app,'active_module')
                    and callable(getattr(app,'can_observe_without_display',None))))

    def offer_result(self, result):
        if not isinstance(result, InferenceResult):
            raise TypeError('Expected the unchanged InferenceResult packet')
        with self._condition:
            if self._stop.is_set() or self._closed: return False
            if result.sequence <= self._highest_offered_sequence:
                self._duplicates += 1
                return False
            self._highest_offered_sequence = result.sequence
            if self._pending is not None: self._replaced += 1
            self._pending = ('result', result)
            self._condition.notify()
            return True

    def offer_manual(self, detections, now):
        now = float(now)
        if not math.isfinite(now): raise ValueError('manual observation time must be finite')
        detections = tuple(detections)
        with self._condition:
            if self._stop.is_set() or self._closed: return False
            self._manual_sequence += 1
            if self._pending is not None: self._replaced += 1
            self._pending = ('manual', (self._manual_sequence, detections, now, self._current_epoch))
            self._condition.notify()
            return True

    def command(self, name, payload=None):
        aliases = {'manualbutton':'button', 'manual_button':'button', 'call_fixture':'fixture'}
        name = aliases.get(name, name)
        if name not in ('load','debug','quit','cancel','button','fixture','voice'):
            raise ValueError('unknown UI command')
        if name == 'fixture' and not callable(payload): raise ValueError('test fixture must be callable')
        if name == 'button' and not isinstance(payload, str): raise ValueError('button command requires its name')
        if name == 'voice' and isinstance(payload,dict):
            payload = dict(payload,queued_at_monotonic=time.monotonic())
        with self._condition:
            if self._stop.is_set() or self._closed: return False
            if len(self._commands) >= 32:
                self._command_drops += 1
                if name != 'quit': return False
                self._commands.pop()
            self._commands.append((name,payload,self._current_epoch))
            self._condition.notify()
            return True

    def latest(self):
        with self._condition: return self._latest

    def acknowledge(self, revision):
        if type(revision) is not int or revision < 0: raise ValueError('revision must be nonnegative integer')
        with self._condition:
            if revision > self._published_revision: return False
            if revision > self._displayed_revision: self._displayed_at = time.perf_counter()
            self._displayed_revision = max(self._displayed_revision, revision)
            self._condition.notify()
            return True

    def status(self):
        with self._condition:
            result = copy.deepcopy(self._status)
            result.update(ready=self._ready, error=self._error, command_error=self._command_error,
                preparing=self._preparing, closed=self._closed,
                accepted_detection_updates=self._accepted, stale_results=self._stale,
                last_result_sequence=self._last_result_sequence,
                published_revision=self._published_revision, displayed_revision=self._displayed_revision,
                waiting_for_display=self._displayed_revision < self._published_revision)
        return result

    def control_status(self):
        """Display-loop flags only; retain detailed statistics off that path."""
        with self._condition:
            return dict(ready=self._ready,stage=self._status['stage'],
                quit_requested=self._status['quit_requested'],
                error=self._error,command_error=self._command_error)

    def metrics(self):
        result = self.status()
        with self._condition:
            result.update(preparations=self._preparations, ticks=self._ticks,
                preparation_attempts=self._preparation_attempts,
                discarded_preparations=self._discarded_preparations,
                replaced_pending_results=self._replaced, duplicate_results=self._duplicates,
                command_drops=self._command_drops, pending_results=int(self._pending is not None),
                inference_attached=self._inference_worker is not None,
                last_attached_sequence=self._last_attached_sequence,
                pending_commands=len(self._commands), command_queue_limit=32,
                mode_epoch=self._current_epoch,epoch_command_drops=self._epoch_command_drops,
                rejected_voice_commands=self._voice_rejections,discarded_placement_metadata=self._metadata_drops,
                max_ui_prepare_hz=15.)
            result['recent_consumed_detections'] = list(self._observation_samples)
            prepare_times, observation_times = list(self._prepare_times), list(self._observation_times)
        result['prepare_ms'] = _stats(prepare_times)
        result['observation_ms'] = _stats(observation_times)
        return result

    def close(self, timeout=3.):
        self._stop.set()
        with self._condition:
            self._pending = None
            self._commands.clear()
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(max(0.,float(timeout)))
        return not self._thread.is_alive()

    def _publish_status(self, app):
        metrics = app.metrics
        snapshot = dict(stage=app.stage, quit_requested=bool(app.quit_requested),
                        app=copy.deepcopy(metrics() if callable(metrics) else metrics))
        renderer_metrics = getattr(app.renderer, 'metrics', None)
        if callable(renderer_metrics): snapshot['renderer'] = renderer_metrics()
        with self._condition: self._status = snapshot

    def _execute(self, app, command, now):
        name, payload = command[:2]
        if len(command) > 2 and command[2] != getattr(app,'module_epoch',0):
            with self._condition:
                self._command_drops += 1
                self._epoch_command_drops += 1
            return False
        if name == 'load':
            app.model.load(Path(payload) if payload is not None else app.work_path)
            app._switch('scene')
            app.selected_id = app.model.selected_id
        elif name == 'debug': app.debug = not app.debug if payload is None else bool(payload)
        elif name == 'quit': app.quit_requested = True
        elif name == 'cancel': app.cancel_interaction(now)
        elif name == 'button': app._button(payload,now)
        elif name == 'voice':
            from combined.voice import valid_voice_payload
            # Audio timestamps use monotonic, not the control perf_counter clock.
            accepts_voice = getattr(app,'accepts_voice',None)
            if not valid_voice_payload(payload) or (callable(accepts_voice) and not accepts_voice(payload)):
                with self._condition: self._voice_rejections += 1
                return False
            app.apply_voice_command(payload['label'], now)
        elif name == 'fixture': payload(app)  # Explicit benchmark/test-only hook; runs on this owner.
        self._sync_interaction_context(app)
        return True

    def _filter_placement_metadata(self, app, result):
        if not callable(getattr(app,'accepts_inference',None)): return result
        epoch, boundary = result.mode_epoch,getattr(app,'transition_perf',float('-inf'))
        def valid_sample(sample):
            return (sample.mode_epoch == epoch and math.isfinite(sample.captured_at)
                    and sample.captured_at >= boundary)
        observations = result.placement_observations
        if not all(valid_sample(sample) for sample in observations): observations = ()
        cycle = result.placement_cycle
        if cycle is not None and (cycle.mode_epoch != epoch or not math.isfinite(cycle.released_at)
                or cycle.released_at < boundary or not all(valid_sample(sample) for sample in cycle.pinches)):
            cycle = None
        if cycle is result.placement_cycle and observations is result.placement_observations: return result
        with self._condition: self._metadata_drops += 1
        return replace(result,placement_cycle=cycle,placement_observations=observations)

    def _consume(self, app, item, now):
        kind, value = item
        if kind == 'result':
            with self._condition: self._last_result_sequence = value.sequence
            accepts_inference = getattr(app,'accepts_inference',None)
            if callable(accepts_inference) and not accepts_inference(value):
                with self._condition:
                    self._stale += 1
                    self._observation_samples.append(dict(sequence=value.sequence,accepted=False,
                        mode_epoch=value.mode_epoch,consumed_at=now,rejection='mode_epoch_or_transition'))
                return
            valid_time = math.isfinite(value.captured_at) and -.1 <= now-value.captured_at <= 1.
            if value.error:
                app.cancel_interaction(now)
                with self._condition: self._error = value.error
                return
            if value.frame_shape != (self.height,self.width,3) or not valid_time:
                with self._condition:
                    self._stale += 1
                    self._observation_samples.append(dict(sequence=value.sequence,accepted=False,
                        consumed_at=now,age_ms=(now-value.captured_at)*1000. if math.isfinite(value.captured_at) else None,
                        rejection='frame_shape' if value.frame_shape!=(self.height,self.width,3) else 'capture_age',
                        detections=_detection_sample(value.detections)))
                return
            detections = value.detections
            value = self._filter_placement_metadata(app,value)
        else:
            _, detections, timestamp = value[:3]
            if not -.1 <= now-timestamp <= 1. or (len(value)>3 and value[3] != getattr(app,'module_epoch',0)):
                with self._condition: self._stale += 1
                return
        started = time.perf_counter()
        observe_inference = getattr(app, 'observe_inference', None)
        observe_sequenced = getattr(app, 'observe_sequenced', None)
        if kind == 'result' and callable(observe_inference): events = observe_inference(value, now)
        elif kind == 'result' and callable(observe_sequenced): events = observe_sequenced(detections,now,value.sequence)
        else: events = app.observe(detections, now)
        self._sync_interaction_context(app)
        with self._condition:
            self._accepted += 1
            self._observation_times.append((time.perf_counter()-started)*1000.)
            self._observation_samples.append(dict(sequence=value.sequence if kind=='result' else value[0],
                accepted=True,
                mode_epoch=value.mode_epoch if kind=='result' else getattr(app,'module_epoch',0),
                consumed_at=now,age_ms=(now-value.captured_at)*1000. if kind=='result' else 0.,
                detections=_detection_sample(detections),
                events=[event.get('kind') for event in events or ()],
                phase=getattr(getattr(app,'controls',None),'phase',None),
                source=getattr(getattr(app,'controls',None),'_source',None),
                rotation_stop_reason=getattr(getattr(app,'spatial',None),'rotation_stop_reason',None)))

    def _run(self):
        app = None
        try:
            factory = self._app_factory
            if factory is None:
                from combined.serial_app import FourLevelsApp
                factory = FourLevelsApp
            app = factory(self.width,self.height,root=self.root,
                          font_path=self.config.get('font_path'),work_path=self.work_path)
            last_key, last_tick = None, 0.
            last_prepare_started = None
            self._sync_interaction_context(app)
            while not self._stop.is_set():
                now = time.perf_counter()
                self._sync_interaction_context(app)
                view_pipeline = self._uses_view_pipeline(app)
                with self._condition:
                    attached, after = self._inference_worker, self._last_attached_sequence
                if attached is not None:
                    result = attached.poll(after)
                    if result is not None:
                        self.offer_result(result)
                        with self._condition: self._last_attached_sequence = result.sequence
                with self._condition:
                    displayed = self._displayed_revision
                    geometry = self._prepared_scenes.get(displayed)
                    displayed_at = self._displayed_at
                if view_pipeline and geometry is not None and displayed > self._geometry_applied_revision:
                    apply_scene = getattr(app, 'set_displayed_scene', None)
                    if callable(apply_scene): apply_scene(geometry[0], displayed, displayed_at, geometry[1], geometry[2])
                    self._geometry_applied_revision = displayed
                    with self._condition:
                        self._prepared_scenes = {revision:scene for revision,scene in self._prepared_scenes.items()
                                                 if revision >= displayed}
                with self._condition:
                    commands = tuple(self._commands)
                    self._commands.clear()
                    item = None
                    pending_detections = ()
                    if self._ready and self._error is None:
                        presented = not self._preparing and self._displayed_revision >= self._published_revision
                        view_input = getattr(app, 'can_observe_without_display', None) if view_pipeline else None
                        pending_detections = (self._pending[1].detections if self._pending and self._pending[0] == 'result'
                                              else self._pending[1][1] if self._pending else ())
                        if presented or (self._pending is not None and callable(view_input) and view_input(pending_detections)):
                            item, self._pending = self._pending, None
                for command in commands:
                    try:
                        self._execute(app,command,now)
                        self._sync_interaction_context(app)
                        with self._condition: self._command_error = None
                    except Exception as exc:
                        app.cancel_interaction(now)
                        self._sync_interaction_context(app)
                        with self._condition: self._command_error = '%s: %s' % (type(exc).__name__,exc)
                if view_pipeline and item is None and self._pending is not None:
                    pause_view = getattr(app,'pause_unpresented_input',None)
                    if callable(pause_view): pause_view(pending_detections,now)
                if commands and item is not None:
                    # A keyboard/test command may replace the entire layout.
                    # A detection dequeued against the former displayed layout
                    # cannot be applied to this newly commanded state.
                    with self._condition: self._stale += 1
                    item = None
                state_changed = bool(commands) or item is not None
                if item is not None: self._consume(app,item,time.perf_counter())
                now = time.perf_counter()
                if now-last_tick >= 1./30.:
                    app.tick(now)
                    self._sync_interaction_context(app)
                    last_tick = now
                    state_changed = True
                    with self._condition: self._ticks += 1
                key = app.renderer.visual_key(app,time.perf_counter())
                # One extra immutable motion frame may overlap the previous
                # page's flush/fence. Picking still waits for actual display.
                motion = bool(getattr(getattr(app,'spatial',None),'continuous_rotating',False))
                depth = 2 if motion and getattr(self._latest,'native_opaque',False) else 1
                view_pipeline = self._uses_view_pipeline(app)
                with self._condition:
                    render_available = not view_pipeline or self._published_revision-self._displayed_revision < depth
                if (last_key is None or key != last_key) and render_available:
                    with self._condition:
                        self._preparing = True
                        revision = self._published_revision+1
                    remaining = (0. if last_prepare_started is None else
                                 last_prepare_started+1./15.-time.perf_counter())
                    if remaining > 0 and self._stop.wait(remaining): break
                    started = time.perf_counter()
                    last_prepare_started = started
                    guard = getattr(app.renderer, 'presentation_guard_key', None)
                    guard_key = guard(app, started) if callable(guard) else key
                    if self._prepare_delay and self._stop.wait(self._prepare_delay): break
                    packet = app.renderer.prepare(app,time.perf_counter(),revision)
                    # Advance elapsed-time safety BEFORE publishing a layer.
                    # A slow preparation must not flash a drag preview which
                    # already expired while that preparation was running.
                    finished = time.perf_counter()
                    app.tick(finished)
                    self._sync_interaction_context(app)
                    current_key = (guard(app, finished) if callable(guard)
                                   else app.renderer.visual_key(app, finished))
                    with self._condition:
                        self._ticks += 1
                        self._preparation_attempts += 1
                        self._prepare_times.append((finished-started)*1000.)
                    if current_key != guard_key:
                        with self._condition:
                            self._discarded_preparations += 1
                            # A rejected draw may already have replaced mutable
                            # geometry/ID buffers. Even a restored visual key
                            # cannot prove that those hit buffers match the old
                            # displayed packet. Rebuild the current state, then
                            # require its new presentation acknowledgement.
                            self._preparing = True
                        last_key = None
                        self._publish_status(app)
                        continue
                    with self._condition:
                        self._latest = packet
                        self._published_revision = revision
                        spatial = getattr(app,'spatial',None)
                        if view_pipeline:
                            self._prepared_scenes[revision] = (getattr(app.renderer, 'prepared_scene', None),
                                getattr(app, 'mode_revision', None),
                                ((spatial.yaw, spatial.pitch, spatial.scale) if spatial is not None else None))
                        self._ready, self._preparing = True, False
                        self._preparations += 1
                    last_key = key
                    state_changed = True
                if state_changed:self._publish_status(app)
                with self._condition:
                    if not self._stop.is_set(): self._condition.wait(1./120.)
        except Exception as exc:
            if app is not None:
                try: app.cancel_interaction(time.perf_counter())
                except Exception: pass
            with self._condition: self._error = '%s: %s' % (type(exc).__name__,exc)
        finally:
            with self._condition:
                self._preparing = False
                self._closed = True
                self._condition.notify_all()
