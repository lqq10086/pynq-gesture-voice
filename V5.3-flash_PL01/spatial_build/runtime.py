"""Bounded asynchronous capture and inference for an independent display loop.

Workers own their runner/source. Images offered to inference are already in the
application's mirrored BGR coordinate space; this module never flips images.
There is one pending image and one completed result, with no accumulating queue.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time

import numpy as np

from gesture_app.inference import NAMES_2, NAMES_3, create_runner, decode, preprocess


@dataclass(frozen=True)
class PlacementObservation:
    sequence: int
    captured_at: float
    detections: tuple
    mode_epoch: int = 0


@dataclass(frozen=True)
class PlacementCycle:
    pinches: tuple
    release_sequence: int
    released_at: float
    mode_epoch: int = 0


@dataclass(frozen=True)
class InferenceResult:
    sequence: int
    detections: tuple
    frame_shape: tuple
    captured_at: float
    finished_at: float
    inference_ms: float
    error: str | None
    source_sequence: int
    started_at: float
    pipeline_ms: float
    placement_cycle: PlacementCycle | None = None
    placement_observations: tuple = ()
    mode_epoch: int = 0


@dataclass(frozen=True)
class DeferredFrame:
    """Transfer one private immutable RGB565 allocation without expanding it."""
    raw: np.ndarray
    shape: tuple
    converter: object
    source_frame_id: int | None = None
    source_complete_monotonic: float | None = None

    def __post_init__(self):
        if (len(self.shape)!=3 or self.shape[2]!=3 or min(self.shape[:2])<=0
                or not isinstance(self.raw,np.ndarray)
                or self.raw.dtype not in (np.dtype('uint8'),np.dtype('<u2'))
                or self.raw.nbytes!=self.shape[0]*self.shape[1]*2
                or not self.raw.flags.c_contiguous or not self.raw.flags.owndata
                or self.raw.flags.writeable or not callable(self.converter)):
            raise ValueError('Deferred source must transfer a private immutable RGB565 allocation')

    def to_bgr(self):
        image=self.converter(self.raw)
        if (not isinstance(image,np.ndarray) or image.dtype!=np.uint8
                or image.shape!=self.shape or not image.flags.c_contiguous
                or not image.flags.owndata or np.shares_memory(image,self.raw)):
            raise ValueError('Deferred conversion must return a private contiguous BGR allocation')
        image.setflags(write=False)
        return image


@dataclass(frozen=True)
class FrameSnapshot:
    sequence: int
    image: np.ndarray
    captured_at: float
    deferred: DeferredFrame | None = None


def _frame_copy(image):
    if (not isinstance(image, np.ndarray) or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
            or min(image.shape[:2]) <= 0):
        raise ValueError("Expected a nonempty uint8 HWC BGR frame")
    return image.copy(order="C")


def _rate(count, first, last):
    return ((count - 1) / (last - first)
            if count > 1 and first is not None and last > first else 0.0)


def _stats(values):
    if not values:
        return {"samples": 0, "mean": 0.0, "p95": 0.0, "maximum": 0.0}
    data = np.asarray(values, dtype=np.float64)
    return {"samples": len(values), "mean": float(data.mean()),
            "p95": float(np.percentile(data, 95)), "maximum": float(data.max())}


class AsyncGestureWorker:
    """A latest-only worker using the unchanged YOLO preprocessing and decode.

    ``config`` is the existing ``inference`` configuration sub-dictionary.
    A supplied runner implements ``infer(tensor)`` and ``close()`` and is also
    owned by this worker. ``poll`` never waits for inference. The interval is a
    minimum between work starts, not an extra sleep after a slow inference.
    Model/contract failures are reported once and stop the worker safely.
    """

    def __init__(self, config=None, root=None, runner=None, names=NAMES_3,
                 interval_ms=200.0, postprocess=None):
        interval_ms = float(interval_ms)
        if not math.isfinite(interval_ms) or interval_ms < 0:
            raise ValueError("interval_ms must be finite and nonnegative")
        if tuple(names) not in (NAMES_2, NAMES_3):
            raise ValueError("Expected the two-class model or an explicit legacy comparison model")
        if runner is None:
            if config is None:
                raise ValueError("An inference config or runner is required")
            runner, names = create_runner(config, root or Path.cwd())
        self.runner = runner
        self.names = tuple(names)
        self.interval_ms = interval_ms
        self.postprocess = dict(postprocess or {})
        self._condition = threading.Condition()
        self._mode_epoch, self._collect_placement, self._context_revision = 0, True, 0
        self._stop = False
        self._finished = False
        self._busy = False
        self._pending = None
        self._latest = None
        self._offered = self._replaced = self._processed = self._successful = 0
        self._first_result_at = self._last_result_at = None
        self._last_started_at = None
        self._network_times = deque(maxlen=512)
        self._pipeline_times = deque(maxlen=512)
        self._intervals = deque(maxlen=512)
        self._error = self._close_error = None
        self._thread = threading.Thread(target=self._run, name="spatial-gesture", daemon=True)
        self._thread.start()

    @property
    def finished(self):
        with self._condition:
            return self._finished

    @property
    def error(self):
        with self._condition:
            return self._error

    def offer(self, mirrored_bgr, captured_at=None):
        """Copy and replace the pending frame; return its submission sequence.

        The copy protects inference from reusable capture/render buffers. This
        does no preprocessing or model work and never waits for the runner.
        Call once per newly captured image, not once per display repaint.
        """
        captured_at = time.perf_counter() if captured_at is None else float(captured_at)
        if not math.isfinite(captured_at):
            raise ValueError("captured_at must be finite")
        image = _frame_copy(mirrored_bgr)
        with self._condition:
            if self._stop or self._finished:
                return None
            self._offered += 1
            if self._pending is not None:
                self._replaced += 1
            self._pending = (self._offered, image, captured_at)
            self._condition.notify()
            return self._offered

    def poll(self, last_sequence=0):
        """Return only a newer completed result, or None immediately."""
        with self._condition:
            return (self._latest if self._latest is not None
                    and self._latest.sequence > last_sequence else None)

    def set_interaction_context(self, epoch, collect_placement):
        if type(epoch) is not int or epoch < 0 or type(collect_placement) is not bool:
            raise ValueError('interaction context requires a nonnegative integer epoch and bool collector flag')
        with self._condition:
            if (epoch,collect_placement) == (self._mode_epoch,self._collect_placement):
                return False
            self._mode_epoch, self._collect_placement = epoch, collect_placement
            self._context_revision += 1
            self._pending = self._latest = None
            if hasattr(self,'_placement_pinches'): self._placement_pinches.clear()
            if hasattr(self,'_placement_cycle'): self._placement_cycle = None
            self._condition.notify_all()
            return True

    def metrics(self):
        """Measured inference updates, never display interpolation counts."""
        with self._condition:
            return {"offered_frames": self._offered,
                    "replaced_pending_frames": self._replaced,
                    "inference_results": self._processed,
                    "successful_results": self._successful,
                    "inference_updates_hz": _rate(self._successful, self._first_result_at, self._last_result_at),
                    "configured_interval_ms": self.interval_ms,
                    "mode_epoch": self._mode_epoch,
                    "collect_placement": self._collect_placement,
                    "placement_pinches_buffered": len(getattr(self,'_placement_pinches',())),
                    "placement_cycle_buffered": getattr(self,'_placement_cycle',None) is not None,
                    "network_ms": _stats(self._network_times),
                    "pipeline_ms": _stats(self._pipeline_times),
                    "start_interval_ms": _stats(self._intervals),
                    "busy": self._busy, "pending": self._pending is not None,
                    "finished": self._finished, "error": self._error,
                    "close_error": self._close_error}

    snapshot_metrics = metrics

    def close(self, timeout=3.0):
        """Request stop; let in-flight driver work finish before runner.close.

        Returns False if a driver is still busy after timeout. Hardware calls
        cannot safely be force-killed; the thread is daemonized in that case.
        """
        with self._condition:
            self._stop = True
            self._pending = None
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(max(0.0, float(timeout)))
        return not self._thread.is_alive()

    def _run(self):
        try:
            while True:
                with self._condition:
                    while not self._stop:
                        if self._pending is None:
                            self._condition.wait()
                            continue
                        remaining = (0.0 if self._last_started_at is None else
                                     self._last_started_at + self.interval_ms / 1000.0 - time.perf_counter())
                        if remaining > 0:
                            self._condition.wait(remaining)
                            continue
                        sequence, image, captured_at = self._pending
                        mode_epoch = self._mode_epoch
                        self._pending = None
                        started_at = time.perf_counter()
                        if self._last_started_at is not None:
                            self._intervals.append((started_at - self._last_started_at) * 1000.0)
                        self._last_started_at = started_at
                        self._busy = True
                        break
                    if self._stop:
                        break
                inference_ms = 0.0
                detections = ()
                error = None
                try:
                    tensor, geometry = preprocess(image)
                    inference_started = time.perf_counter()
                    raw = self.runner.infer(tensor)
                    inference_ms = (time.perf_counter() - inference_started) * 1000.0
                    detections = tuple(decode(raw, geometry, names=self.names, **self.postprocess))
                except Exception as exc:
                    error = "%s: %s" % (type(exc).__name__, exc)
                finished_at = time.perf_counter()
                pipeline_ms = (finished_at - started_at) * 1000.0
                with self._condition:
                    self._busy = False
                    self._processed += 1
                    self._latest = InferenceResult(self._processed, detections, tuple(image.shape),
                                                   captured_at, finished_at, inference_ms, error,
                                                   sequence, started_at, pipeline_ms,mode_epoch=mode_epoch)
                    self._network_times.append(inference_ms)
                    self._pipeline_times.append(pipeline_ms)
                    if error is None:
                        self._successful += 1
                        if self._first_result_at is None:
                            self._first_result_at = finished_at
                        self._last_result_at = finished_at
                    else:
                        self._error = error
                        self._pending = None
                        self._stop = True
        finally:
            try:
                self.runner.close()
            except Exception as exc:
                with self._condition:
                    self._close_error = "%s: %s" % (type(exc).__name__, exc)
            with self._condition:
                self._busy = False
                self._finished = True
                self._condition.notify_all()


class LatestFrameSource:
    """Read an existing ``read_frame`` / ``close`` source off the UI thread.

    ``captured_at`` measures when read_frame returned, not sensor exposure time.
    snapshot returns the latest source image once per caller sequence. Its
    buffer is never reused by this worker. A slow/blocking source cannot stall
    snapshot or the application's independent display loop.
    """

    def __init__(self, source, defer_conversion=False, on_demand=False):
        self.source = source
        self.defer_conversion = bool(defer_conversion)
        self.on_demand = bool(on_demand)
        self._lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._demand = threading.Event()
        self._requested = False
        self._latest = None
        self._captured = 0
        self._first_capture_at = self._last_capture_at = None
        self._finished = False
        self._error = self._close_error = None
        self._source_closed = False
        self._thread = threading.Thread(target=self._run, name="spatial-capture", daemon=True)
        self._thread.start()

    @property
    def finished(self):
        with self._lock:
            return self._finished

    @property
    def error(self):
        with self._lock:
            return self._error

    def snapshot(self, last_sequence=0):
        """Return a newer captured frame only; cached rendering is not capture."""
        with self._lock:
            if self._latest is not None and self._latest.sequence > last_sequence:
                return self._latest
            if self.on_demand and not self._stop.is_set() and not self._requested:
                self._requested = True
                self._demand.set()
            return None

    def metrics(self):
        with self._lock:
            return {"captured_frames": self._captured,
                    "capture_updates_hz": _rate(self._captured, self._first_capture_at, self._last_capture_at),
                    "finished": self._finished, "error": self._error,
                    "close_error": self._close_error,
                    "deferred_conversion": self.defer_conversion,
                    "capture_policy": "on_demand" if self.on_demand else "continuous",
                    "timestamp_scope": "read_frame delivery, not sensor exposure"}

    snapshot_metrics = metrics

    def _close_source(self):
        with self._close_lock:
            if self._source_closed:
                return
            self._source_closed = True
            try:
                self.source.close()
            except Exception as exc:
                with self._lock:
                    self._close_error = "%s: %s" % (type(exc).__name__, exc)

    def close(self, timeout=3.0):
        self._stop.set()
        self._demand.set()
        # Releasing a camera usually unblocks its pending read. No state lock
        # is held while calling external driver cleanup.
        self._close_source()
        if threading.current_thread() is not self._thread:
            self._thread.join(max(0.0, float(timeout)))
        return not self._thread.is_alive()

    def _run(self):
        try:
            while not self._stop.is_set():
                if self.on_demand:
                    self._demand.wait()
                    self._demand.clear()
                    if self._stop.is_set():break
                deferred_read=getattr(self.source,'read_frame_deferred',None) if self.defer_conversion else None
                item=deferred_read() if callable(deferred_read) else self.source.read_frame()
                captured_at = time.perf_counter()
                if item is None or self._stop.is_set():
                    break
                deferred=item if callable(deferred_read) else None
                if deferred is not None:
                    if not isinstance(deferred,DeferredFrame):raise ValueError('Expected DeferredFrame from raw source')
                    image=None
                else:
                    image = _frame_copy(item)
                with self._lock:
                    self._captured += 1
                    if self._first_capture_at is None:
                        self._first_capture_at = captured_at
                    self._last_capture_at = captured_at
                    self._latest = FrameSnapshot(self._captured, image, captured_at, deferred)
                    self._requested = False
        except Exception as exc:
            if not self._stop.is_set():
                with self._lock:
                    self._error = "%s: %s" % (type(exc).__name__, exc)
        finally:
            self._close_source()
            with self._lock:
                self._finished = True
