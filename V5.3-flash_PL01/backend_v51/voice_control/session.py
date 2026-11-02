"""Bounded audio/VAD worker; only the video thread applies UI commands."""
from __future__ import annotations

from collections import deque
from dataclasses import fields
from pathlib import Path
import threading
import time

import numpy as np

from .audio_source import create_audio_source
from .classifier import KwsClassifier
from .vad import EnergyVad, VadConfig, VadSegmenter, prime_from_waveform

COMMANDS = {
    'one': ('OPEN_APP', 'tic_tac_toe'), 'two': ('OPEN_APP', 'memory_match'),
    'three': ('OPEN_APP', 'palm_bounce'), 'four': ('OPEN_APP', 'block_builder'),
    'on': ('OPEN_MENU', None), 'off': ('CLOSE_WINDOW', None),
    'back': ('BACK', None), 'stop': ('END_SESSION', None),
}
POLICIES = ('exclusive', 'gated', 'always')


class InferenceGate:
    """Short state handshake, never a mutex held during PL/CPU computation."""
    def __init__(self):
        self._condition = threading.Condition()
        self._gesture_active = False
        self._voice_active = False
        self._voice_waiters = 0

    def try_begin_gesture(self):
        with self._condition:
            if self._gesture_active or self._voice_active or self._voice_waiters:
                return False
            self._gesture_active = True
            return True

    def end_gesture(self):
        with self._condition:
            self._gesture_active = False
            self._condition.notify_all()

    def begin_voice(self, cancelled):
        with self._condition:
            self._voice_waiters += 1
            try:
                while self._gesture_active or self._voice_active:
                    if cancelled():
                        return False
                    self._condition.wait(.02)
                if cancelled():
                    return False
                self._voice_active = True
                return True
            finally:
                self._voice_waiters -= 1

    def end_voice(self):
        with self._condition:
            self._voice_active = False
            self._condition.notify_all()


class SerialGestureRunner:
    def __init__(self, runner, gate):
        self.runner, self.gate = runner, gate

    def infer(self, tensor):
        while not self.gate.try_begin_gesture():
            time.sleep(.002)
        try:
            return self.runner.infer(tensor)
        finally:
            self.gate.end_gesture()

    def close(self):
        if hasattr(self.runner, 'close'):
            self.runner.close()


class KwsSession:
    """start/poll/stop plus explicit arming. No FPGA overlay is touched.

    exclusive: continuous cheap capture/VAD, NN only while manually armed;
    gated: continuous capture/VAD, utterance NN waits for the active PL task;
    always: capture/VAD and utterance NN may overlap gesture inference.
    Neither continuous policy uses neural sliding-window polling.
    """
    def __init__(self, config, root, gate=None, classifier=None, source=None):
        self.config = dict(config)
        self.policy = self.config.get('policy', 'exclusive')
        if self.policy not in POLICIES:
            raise ValueError('voice policy must be exclusive, gated or always')
        self.gate = gate or InferenceGate()
        asset_dir = Path(self.config.get('assets', 'voice_assets'))
        if not asset_dir.is_absolute():
            asset_dir = Path(root) / asset_dir
        self.classifier = classifier or KwsClassifier(asset_dir, self.config.get('backend', 'opencv'), 1)
        try:
            self.source = source or create_audio_source(self.config.get('audio', {'kind': 'sounddevice'}), root)
        except Exception:
            self.classifier.close()
            raise
        rt = self.classifier.config.get('realtime', {})
        vad_values = dict(rt.get('vad', {}))
        vad_values.update(self.config.get('vad', {}))
        allowed = {f.name for f in fields(VadConfig)}
        self.vad_config = VadConfig(**{k: v for k, v in vad_values.items() if k in allowed})
        if self.vad_config.sample_rate != 16000 or self.vad_config.frame_length != 480:
            raise ValueError('Voice session requires 16 kHz and 30 ms VAD frames')
        self.vad = EnergyVad(self.vad_config)
        self.segmenter = VadSegmenter(self.vad, self.vad_config)
        self.frame_length = self.vad_config.frame_length
        self.min_utterance_ms = float(self.config.get('min_utterance_ms', 300))
        self.max_audio_age_ms = float(self.config.get('max_audio_age_ms', 180))
        self.max_event_age_ms = float(self.config.get('max_event_age_ms', 200))
        self.max_utterance_age_ms = float(self.config.get('max_utterance_age_ms', 1200))
        self.session_timeout_s = float(self.config.get('session_timeout_s', 8))
        self.command_deadline_ms = float(self.config.get('command_deadline_ms', 500))
        self.cooldown_ms = float(self.config.get('cooldown_ms', 900))
        # thresholds.json already calibrates each word. Do not override lower
        # two/four/back thresholds with an implicit global 0.55 gate.
        self.min_confidence = float(self.config.get('minimum_confidence', 0))
        self.word_confidence = dict(self.config.get('command_confidence', {}))
        if (not 0 <= self.min_confidence <= 1 or not set(self.word_confidence).issubset(COMMANDS)
                or any(not 0 <= float(v) <= 1 for v in self.word_confidence.values())):
            raise ValueError('Invalid command confidence')
        bounds = (self.min_utterance_ms, self.max_audio_age_ms, self.max_event_age_ms,
                  self.cooldown_ms, self.command_deadline_ms, self.max_utterance_age_ms, self.session_timeout_s)
        if any(not np.isfinite(v) or v < 0 for v in bounds):
            raise ValueError('Invalid voice time bound')
        self._state_lock = threading.Lock()
        self._condition = threading.Condition(self._state_lock)
        self._stop = threading.Event()
        self._capture_done = threading.Event()
        self._thread = None
        self._recognizer_thread = None
        self._closed = False
        self._events = deque(maxlen=4)
        self._utterances = deque(maxlen=2)
        self._observations = deque(maxlen=256)
        self._armed = bool(self.config.get('start_listening', self.policy != 'exclusive'))
        self._generation = 0
        self._arm_since = time.monotonic()
        self._busy = False
        self._error = None
        self._last_hit = -1e9
        self._event_id = 0
        self._last_sequence = None
        self._pending = np.empty(0, np.float32)
        self._offline_fast = (self.config.get('audio', {}).get('kind') == 'wav'
                              and not self.config.get('audio', {}).get('realtime', True))
        self._counts = dict(audio_chunks=0, vad_frames=0, segments=0, nn_calls=0,
                            rejected=0, cooldown_rejects=0, discontinuities=0,
                            stale_audio=0, stale_events=0, deadline_rejects=0, event_queue_drops=0,
                            stale_utterances=0, utterance_queue_drops=0, session_timeouts=0,
                            latency_target_misses=0, max_utterance_queue_age_ms=0.,
                            max_audio_queue_age_ms=0., vad_cpu_ms=0., model_cpu_ms=0.)

    @property
    def listening(self):
        self._expire_session()
        with self._state_lock:
            return self._armed

    @property
    def pending(self):
        with self._state_lock:
            return self._busy or bool(self._utterances)

    @property
    def should_pause_gesture(self):
        return ((self.policy == 'exclusive' and self.listening)
                or (self.policy == 'gated' and self.pending))

    @property
    def ready(self):
        return bool(self.vad.calibrated or self.vad._frames_seen > self.vad_config.min_observe_frames)

    @property
    def eof(self):
        return self._capture_done.is_set() and not self.pending

    @property
    def error(self):
        return self._error

    @property
    def stats(self):
        self._expire_session()
        with self._state_lock:
            result = dict(self._counts)
            result.update(policy=self.policy, listening=self._armed, busy=self._busy,
                          ready=self.ready,
                          eof=self._capture_done.is_set() and not self._busy and not self._utterances,
                          error=self._error,
                          event_queue_size=len(self._events), utterance_queue_size=len(self._utterances),
                          observations=list(self._observations))
        result['audio'] = dict(self.source.stats)
        return result

    def drain_observations(self):
        """Main-thread diagnostics; includes accepted, rejected and slow results."""
        with self._state_lock:
            result = [dict(item) for item in self._observations]
            self._observations.clear()
            return result

    def set_listening(self, enabled):
        with self._condition:
            if enabled and (self._error is not None or self._closed or self._stop.is_set()):
                return False
            self._set_listening_locked(enabled)
            return True

    def _set_listening_locked(self, enabled):
        self._armed = bool(enabled)
        self._generation += 1
        self._arm_since = time.monotonic()
        self._events.clear()
        self._utterances.clear()
        self._condition.notify_all()

    def _expire_session(self):
        with self._condition:
            if (self.policy == 'exclusive' and self._armed and self.session_timeout_s
                    and time.monotonic()-self._arm_since >= self.session_timeout_s):
                self._set_listening_locked(False)
                self._counts['session_timeouts'] += 1

    def _fail(self, error):
        with self._condition:
            self._error = str(error)
            self._set_listening_locked(False)
            self._stop.set()

    def start(self):
        if self._thread is not None or self._closed:
            raise RuntimeError('Voice session already started')
        try:
            self.source.start()
            if self.config.get('prime_offline', False):
                if self.config.get('audio', {}).get('kind') != 'wav' or not hasattr(self.source, 'read_all'):
                    raise ValueError('prime_offline is only for complete WAV validation')
                prime_from_waveform(self.vad, self.source.read_all())
            self._arm_since = time.monotonic()
            self._stop.clear()
            self._capture_done.clear()
            self._recognizer_thread = threading.Thread(target=self._recognition_loop,
                                                       name='voice-recognizer', daemon=True)
            self._thread = threading.Thread(target=self._loop, name='voice-vad', daemon=True)
            self._recognizer_thread.start()
            self._thread.start()
        except Exception as exc:
            self._fail(exc)
            self.source.stop()
            raise

    def poll(self):
        self._expire_session()
        with self._condition:
            while self._events:
                event = self._events.popleft()
                if (event['generation'] != self._generation
                        or (time.monotonic() - event['created_at']) * 1000 > self.max_event_age_ms):
                    self._counts['stale_events'] += 1
                    continue
                if self._utterance_expired(event['utterance_ready_at']):
                    self._counts['stale_utterances'] += 1
                    continue
                event['polled_at'] = time.monotonic()
                event['event_wait_ms'] = max(0., (event['polled_at']-event['created_at'])*1000)
                if self.policy == 'exclusive' or event['label'] == 'stop':
                    self._set_listening_locked(False)
                return event
        return None

    def _reset_segment(self):
        self.segmenter = VadSegmenter(self.vad, self.vad_config)
        self._pending = np.empty(0, np.float32)

    def _utterance_expired(self, ready_at):
        # Queue age starts at completed VAD READY, never window beginning.
        # 500 ms end-to-event is a recorded target, not a filter for slow data.
        return (not self._offline_fast
                and (time.monotonic() - ready_at) * 1000 > self.max_utterance_age_ms)

    def _queue_segment(self, segment, ended_at, ready_at, generation, allow_forced=False):
        self._counts['segments'] += 1
        if segment.duration*1000 < self.min_utterance_ms or (segment.forced and not allow_forced):
            return
        with self._condition:
            if not self._armed or generation != self._generation:
                return
            if len(self._utterances) == self._utterances.maxlen:
                self._counts['utterance_queue_drops'] += 1
            self._utterances.append((segment, ended_at, generation, ready_at, allow_forced))
            self._condition.notify_all()

    def _recognition_loop(self):
        try:
            while not self._stop.is_set():
                with self._condition:
                    while not self._utterances and not self._stop.is_set():
                        if self._capture_done.is_set():
                            return
                        self._condition.wait(.05)
                    if self._stop.is_set():
                        return
                    item = self._utterances.popleft()
                    self._busy = True
                segment, ended_at, generation, ready_at, forced = item
                self._recognize(segment, ended_at, generation, forced, ready_at)
        except Exception as exc:
            self._fail(exc)
            self.source.stop()
        finally:
            with self._condition:
                self._busy = False

    def _loop(self):
        generation = -1
        last_end_at = time.monotonic()
        try:
            while not self._stop.is_set():
                self._expire_session()
                chunk = self.source.read(timeout=.05)
                source_stats = self.source.stats
                # Capture overflow is a recoverable discontinuity, while a
                # failed device/process must also be reported at EOF.
                if source_stats.get('errors', 0):
                    raise RuntimeError(source_stats.get('last_error') or 'Audio source failed')
                if chunk is None:
                    if self.source.eof:
                        # Only finite offline files may flush an unfinished word.
                        if self.config.get('audio', {}).get('kind') == 'wav':
                            tail = self.segmenter.flush()
                            if tail is not None:
                                self._queue_segment(tail, last_end_at, last_end_at, generation, True)
                        elif not self._stop.is_set():
                            raise RuntimeError('Audio source ended unexpectedly')
                        break
                    continue
                audio = np.asarray(chunk.audio)
                if (chunk.sample_rate != 16000 or audio.dtype != np.float32 or audio.ndim != 1
                        or not np.isfinite(audio).all() or (audio.size and np.max(np.abs(audio)) > 1.01)):
                    raise ValueError('Audio source must return finite 1D mono float32 in [-1,1] at 16 kHz')
                now = time.monotonic()
                age_ms = max(0., (now - chunk.captured_at) * 1000)
                self._counts['audio_chunks'] += 1
                self._counts['max_audio_queue_age_ms'] = max(self._counts['max_audio_queue_age_ms'], age_ms)
                current_generation = self._generation
                discontinuity = chunk.discontinuous or (self._last_sequence is not None
                                                         and chunk.sequence != self._last_sequence + 1)
                self._last_sequence = chunk.sequence
                if current_generation != generation:
                    self._reset_segment()
                    generation = current_generation
                if discontinuity:
                    self._counts['discontinuities'] += 1
                    self._reset_segment()
                if not self._offline_fast and age_ms > self.max_audio_age_ms:
                    self._counts['stale_audio'] += 1
                    self._reset_segment()
                    continue
                if not self._offline_fast and chunk.captured_at < self._arm_since:
                    self._reset_segment()
                    continue
                self._pending = np.concatenate((self._pending, np.asarray(chunk.audio, np.float32).reshape(-1)))
                while self._pending.size >= self.frame_length:
                    frame = self._pending[:self.frame_length].copy()
                    self._pending = self._pending[self.frame_length:]
                    last_end_at = (now if self._offline_fast else chunk.captured_at
                                   - self._pending.size / 16000.)
                    begin = time.perf_counter()
                    done = self.segmenter.push(frame)
                    self._counts['vad_cpu_ms'] += (time.perf_counter() - begin) * 1000
                    self._counts['vad_frames'] += 1
                    if done is not None:
                        ended_at = last_end_at - (0 if done.forced else self.vad_config.end_frames * .03)
                        self._queue_segment(done, ended_at, last_end_at, generation)
        except Exception as exc:
            self._fail(exc)
            self.source.stop()
        finally:
            self._capture_done.set()
            with self._condition:
                self._condition.notify_all()

    def _recognize(self, segment, ended_at, generation, allow_forced=False, ready_at=None):
        ready_at = time.monotonic() if ready_at is None else ready_at
        if (not self.listening or generation != self._generation
                or segment.duration * 1000 < self.min_utterance_ms
                or (segment.forced and not allow_forced)):
            with self._state_lock:
                self._busy = False
            return
        with self._state_lock:
            self._busy = True
        acquired = False
        lock_begin = time.monotonic()
        try:
            if self.policy != 'always':
                cancelled = lambda: (self._stop.is_set() or not self.listening
                                      or generation != self._generation or self._utterance_expired(ready_at))
                acquired = self.gate.begin_voice(cancelled)
                if not acquired:
                    if self._utterance_expired(ready_at):
                        self._counts['stale_utterances'] += 1
                    return
            if not self.listening or generation != self._generation or self._stop.is_set():
                return
            if self._utterance_expired(ready_at):
                self._counts['stale_utterances'] += 1
                return
            started = time.monotonic()
            queue_age_ms = max(0., (started-ready_at)*1000)
            self._counts['max_utterance_queue_age_ms'] = max(self._counts['max_utterance_queue_age_ms'], queue_age_ms)
            self._counts['nn_calls'] += 1
            result = self.classifier.classify(segment.audio)
            completed = time.monotonic()
            self._counts['model_cpu_ms'] += (completed - started) * 1000
            label, confidence = result['label'], float(result['confidence'])
            threshold = float(self.word_confidence.get(label, self.min_confidence))
            accepted = bool(result.get('is_target') and label in COMMANDS
                            and np.isfinite(confidence) and confidence >= threshold)
            elapsed_ms = max(0., (completed-ended_at)*1000)
            observation = dict(label=label, confidence=confidence, accepted=accepted,
                               mfcc_ms=result.get('mfcc_ms'), network_ms=result.get('network_ms'),
                               lock_wait_ms=(started-lock_begin)*1000, classification_ms=(completed-started)*1000,
                               utterance_queue_age_ms=queue_age_ms, vad_tail_ms=max(0., (ready_at-ended_at)*1000),
                               estimated_end_to_event_ms=elapsed_ms,
                               latency_target_met=elapsed_ms <= self.command_deadline_ms,
                               disposition='rejected', created_at=completed)
            with self._state_lock:
                self._observations.append(observation)
                if accepted and elapsed_ms > self.command_deadline_ms:
                    self._counts['latency_target_misses'] += 1
                if self._utterance_expired(ready_at):
                    self._counts['stale_utterances'] += 1
                    observation['disposition'] = 'stale_utterance'
                    return
                if not accepted:
                    self._counts['rejected'] += 1
                    return
                if not self._armed or generation != self._generation:
                    observation['disposition'] = 'cancelled'
                    return
                if (completed - self._last_hit) * 1000 < self.cooldown_ms and not self._offline_fast:
                    self._counts['cooldown_rejects'] += 1
                    observation['disposition'] = 'cooldown'
                    return
                self._last_hit = completed
                self._event_id += 1
                action, target = COMMANDS[label]
                event = dict(observation, action=action, target=target, event_id=self._event_id,
                             generation=generation, utterance_ended_at=ended_at,
                             utterance_ready_at=ready_at,
                             timing_scope='Estimated PCM speech end to event; excludes wireless/ALSA unknown buffering and display')
                if len(self._events) == self._events.maxlen:
                    self._counts['event_queue_drops'] += 1
                observation['disposition'] = event['disposition'] = 'queued'
                self._events.append(event)
        finally:
            if acquired:
                self.gate.end_voice()
            with self._state_lock:
                self._busy = False

    def stop(self):
        if self._closed:
            return
        self._stop.set()
        with self._condition:
            self._set_listening_locked(False)
        self.source.stop()
        for thread in (self._thread, self._recognizer_thread):
            if thread is not None:
                thread.join(timeout=3.)
                if thread.is_alive():
                    raise RuntimeError('Voice worker did not stop; classifier remains owned by worker')
        self.classifier.close()
        self._closed = True

    close = stop
