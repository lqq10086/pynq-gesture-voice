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
    """One mutex shared by gesture inference and utterance feature/inference."""
    def __init__(self):
        self.lock = threading.Lock()


class SerialGestureRunner:
    def __init__(self, runner, gate):
        self.runner, self.gate = runner, gate

    def infer(self, tensor):
        with self.gate.lock:
            return self.runner.infer(tensor)

    def close(self):
        if hasattr(self.runner, 'close'):
            self.runner.close()


class KwsSession:
    """start/poll/stop plus explicit arming. No FPGA overlay is touched.

    exclusive: continuous cheap capture/VAD, NN only while manually armed;
    gated: continuous capture/VAD, utterance NN shares the gesture mutex;
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
        self.command_deadline_ms = float(self.config.get('command_deadline_ms', 500))
        self.cooldown_ms = float(self.config.get('cooldown_ms', 900))
        # Retain the teammate's live-demo safety gate; calibration is also
        # enforced by the classifier. Set a per-word override explicitly.
        self.min_confidence = float(self.config.get('minimum_confidence', .55))
        self.word_confidence = dict(self.config.get('command_confidence', {}))
        if not 0 <= self.min_confidence <= 1 or any(not 0 <= float(v) <= 1 for v in self.word_confidence.values()):
            raise ValueError('Invalid command confidence')
        if min(self.min_utterance_ms, self.max_audio_age_ms, self.max_event_age_ms, self.cooldown_ms, self.command_deadline_ms) < 0:
            raise ValueError('Invalid voice time bound')
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._events = deque(maxlen=4)
        self._observations = deque(maxlen=256)
        self._armed = bool(self.config.get('start_listening', self.policy != 'exclusive'))
        self._generation = 0
        self._arm_since = 0.0
        self._busy = False
        self._error = None
        self._eof = False
        self._last_hit = -1e9
        self._last_sequence = None
        self._pending = np.empty(0, np.float32)
        self._offline_fast = (self.config.get('audio', {}).get('kind') == 'wav'
                              and not self.config.get('audio', {}).get('realtime', True))
        self._counts = dict(audio_chunks=0, vad_frames=0, segments=0, nn_calls=0,
                            rejected=0, cooldown_rejects=0, discontinuities=0,
                            stale_audio=0, stale_events=0, deadline_rejects=0, event_queue_drops=0,
                            max_audio_queue_age_ms=0., vad_cpu_ms=0., model_cpu_ms=0.)

    @property
    def listening(self):
        with self._state_lock:
            return self._armed

    @property
    def pending(self):
        with self._state_lock:
            return self._busy

    @property
    def ready(self):
        return bool(self.vad.calibrated or self.vad._frames_seen > self.vad_config.min_observe_frames)

    @property
    def eof(self):
        return self._eof

    @property
    def error(self):
        return self._error

    @property
    def stats(self):
        with self._state_lock:
            result = dict(self._counts)
            result.update(policy=self.policy, listening=self._armed, busy=self._busy,
                          ready=self.ready, eof=self._eof, error=self._error,
                          event_queue_size=len(self._events), observations=list(self._observations))
        result['audio'] = dict(self.source.stats)
        return result

    def set_listening(self, enabled):
        with self._state_lock:
            self._armed = bool(enabled)
            self._generation += 1
            self._arm_since = time.monotonic()
            self._events.clear()

    def start(self):
        if self._thread is not None:
            raise RuntimeError('Voice session already started')
        self.source.start()
        try:
            if self.config.get('prime_offline', False):
                if self.config.get('audio', {}).get('kind') != 'wav' or not hasattr(self.source, 'read_all'):
                    raise ValueError('prime_offline is only for complete WAV validation')
                prime_from_waveform(self.vad, self.source.read_all())
            self._arm_since = time.monotonic()
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='voice-kws', daemon=True)
            self._thread.start()
        except Exception:
            self.source.stop()
            raise

    def poll(self):
        with self._state_lock:
            while self._events:
                event = self._events.popleft()
                if (event['generation'] != self._generation
                        or (time.monotonic() - event['created_at']) * 1000 > self.max_event_age_ms):
                    self._counts['stale_events'] += 1
                    continue
                if self._deadline_passed(event['utterance_ended_at']):
                    self._counts['deadline_rejects'] += 1
                    continue
                return event
        return None

    def _reset_segment(self):
        self.segmenter = VadSegmenter(self.vad, self.vad_config)
        self._pending = np.empty(0, np.float32)

    def _deadline_passed(self, ended_at):
        # Fast file replay is a functional test, not a real-time contract.
        return (not self._offline_fast
                and (time.monotonic() - ended_at) * 1000 > self.command_deadline_ms)

    def _loop(self):
        generation = self._generation
        last_end_at = time.monotonic()
        try:
            while not self._stop.is_set():
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
                                self._recognize(tail, last_end_at, generation, allow_forced=True)
                        self._eof = True
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
                        self._recognize(done, ended_at, generation)
        except Exception as exc:
            self._error = str(exc)
        finally:
            self._busy = False

    def _recognize(self, segment, ended_at, generation, allow_forced=False):
        self._counts['segments'] += 1
        if (not self.listening or generation != self._generation
                or segment.duration * 1000 < self.min_utterance_ms
                or (segment.forced and not allow_forced)):
            return
        with self._state_lock:
            self._busy = True
        acquired = False
        lock_begin = time.monotonic()
        try:
            if self.policy != 'always':
                while not self._stop.is_set():
                    if self._deadline_passed(ended_at):
                        self._counts['deadline_rejects'] += 1
                        return
                    if self.gate.lock.acquire(timeout=.05):
                        acquired = True
                        break
                if not acquired:
                    return
            if not self.listening or generation != self._generation or self._stop.is_set():
                return
            if self._deadline_passed(ended_at):
                self._counts['deadline_rejects'] += 1
                return
            started = time.monotonic()
            self._counts['nn_calls'] += 1
            result = self.classifier.classify(segment.audio)
            completed = time.monotonic()
            self._counts['model_cpu_ms'] += (completed - started) * 1000
            label, confidence = result['label'], float(result['confidence'])
            threshold = float(self.word_confidence.get(label, self.min_confidence))
            accepted = bool(result.get('is_target') and label in COMMANDS and confidence >= threshold)
            observation = dict(label=label, confidence=confidence, accepted=accepted,
                               sequence=self._counts['nn_calls'],
                               raw_argmax=result.get('raw_argmax',label),
                               classifier_threshold=result.get('threshold'),
                               command_threshold=threshold, reason='pending',
                               mfcc_ms=result.get('mfcc_ms'), network_ms=result.get('network_ms'),
                               lock_wait_ms=(started-lock_begin)*1000, classification_ms=(completed-started)*1000)
            with self._state_lock:
                self._observations.append(observation)
                if self._deadline_passed(ended_at):
                    observation['reason'] = 'deadline'
                    self._counts['deadline_rejects'] += 1
                    return
                if not accepted:
                    observation['reason'] = 'not_target' if not result.get('is_target') else 'command_threshold'
                    self._counts['rejected'] += 1
                    return
                if not self._armed or generation != self._generation:
                    observation['reason'] = 'not_listening'
                    return
                if (completed - self._last_hit) * 1000 < self.cooldown_ms and not self._offline_fast:
                    observation['reason'] = 'cooldown'
                    self._counts['cooldown_rejects'] += 1
                    return
                self._last_hit = completed
                observation['reason'] = 'queued'
                action, target = COMMANDS[label]
                event = dict(label=label, confidence=confidence, action=action, target=target,
                             generation=generation, utterance_ended_at=ended_at,
                             created_at=completed, classification_ms=observation['classification_ms'],
                             lock_wait_ms=observation['lock_wait_ms'],
                             timing_scope='Estimated speech end -> frame ready; excludes physical display')
                if len(self._events) == self._events.maxlen:
                    self._counts['event_queue_drops'] += 1
                self._events.append(event)
        finally:
            if acquired:
                self.gate.lock.release()
            with self._state_lock:
                self._busy = False

    def stop(self):
        self._stop.set()
        self.source.stop()
        if self._thread is not None:
            self._thread.join(timeout=3.)
            if self._thread.is_alive():
                raise RuntimeError('Voice worker did not stop; classifier remains owned by worker')
        self.classifier.close()

    close = stop
