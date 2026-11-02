"""Nonblocking 16 kHz PCM16 audio sources for the voice worker.

Capture callbacks/readers only copy PCM into a bounded queue.  Conversion to
float32 happens in ``read``; feature extraction and inference belong to the
consumer.  This module never opens a microphone merely by being imported.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Union
import queue
import subprocess
import threading
import time
import wave

import numpy as np


TARGET_SAMPLE_RATE = 16000
DEFAULT_BLOCK_MS = 30


@dataclass
class AudioChunk:
    audio: np.ndarray
    sample_rate: int
    sequence: int
    captured_at: float
    discontinuous: bool = False


def _block_samples(sample_rate: int, block_ms: float) -> int:
    if int(sample_rate) != TARGET_SAMPLE_RATE:
        raise ValueError("Voice capture requires 16000 Hz; implicit resampling is disabled.")
    samples = float(block_ms) * TARGET_SAMPLE_RATE / 1000.0
    if not np.isfinite(samples) or samples <= 0 or abs(samples - round(samples)) > 1e-6:
        raise ValueError("block_ms must give a positive whole number of 16000 Hz samples.")
    return int(round(samples))


def _timeout(value: float) -> float:
    value = float(value)
    if not np.isfinite(value) or value < 0:
        raise ValueError("Audio read timeout must be finite and nonnegative.")
    return value


def _pcm16_to_float(raw: bytes, channels: int = 1) -> np.ndarray:
    values = np.frombuffer(raw, dtype="<i2").astype(np.float32)
    if channels > 1:
        values = values.reshape((-1, channels)).mean(axis=1, dtype=np.float32)
    values *= np.float32(1.0 / 32768.0)
    return np.ascontiguousarray(values, dtype=np.float32)


class WavSource:
    """Stream a PCM16 WAV in 30 ms blocks, optionally paced by monotonic time.

    ``read_all`` reads the complete file without consuming the stream.  It is
    intended for offline tests or estimating the VAD noise floor.  Stereo WAVs
    are averaged to mono; files with another rate/encoding are rejected.
    """

    def __init__(self, path: Union[str, Path], realtime: bool = True) -> None:
        self.path = Path(path)
        self.realtime = bool(realtime)
        self.sample_rate = TARGET_SAMPLE_RATE
        self.block_samples = 480
        self._wave = None  # type: Optional[wave.Wave_read]
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._eof = False
        self._started = False
        self._sequence = 0
        self._frames_read = 0
        self._channels = 1
        self._total_frames = 0
        self._started_at = 0.0
        self._stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats() -> Dict[str, Any]:
        return {"chunks_captured": 0, "chunks_read": 0, "dropped_blocks": 0,
                "discontinuities": 0, "errors": 0, "last_error": None}

    @staticmethod
    def _validate(reader: wave.Wave_read) -> None:
        if reader.getframerate() != TARGET_SAMPLE_RATE:
            raise ValueError("WAV must be 16000 Hz. Convert the file before voice recognition.")
        if reader.getsampwidth() != 2 or reader.getcomptype() != "NONE":
            raise ValueError("WAV must contain uncompressed signed PCM16 audio.")
        if reader.getnchannels() not in (1, 2):
            raise ValueError("WAV must have one or two channels (stereo is averaged to mono).")

    @property
    def eof(self) -> bool:
        return self._eof

    @property
    def stats(self) -> Dict[str, Any]:
        result = dict(self._stats)
        result.update({"queue_size": 0, "queue_capacity": 0, "frames_read": self._frames_read})
        return result

    def start(self) -> None:
        if self._started and not self._stop.is_set():
            return
        reader = wave.open(str(self.path), "rb")
        try:
            self._validate(reader)
        except Exception:
            reader.close()
            raise
        with self._lock:
            self._wave = reader
            self._channels = reader.getnchannels()
            self._total_frames = reader.getnframes()
            self._frames_read = 0
            self._sequence = 0
            self._stats = self._fresh_stats()
            self._started_at = time.monotonic()
            self._eof = self._total_frames == 0
            self._started = True
            self._stop.clear()

    def read_all(self) -> np.ndarray:
        """Return float32 mono samples without changing playback position."""
        with wave.open(str(self.path), "rb") as reader:
            self._validate(reader)
            return _pcm16_to_float(reader.readframes(reader.getnframes()), reader.getnchannels())

    def read(self, timeout: float = 0.05) -> Optional[AudioChunk]:
        timeout = _timeout(timeout)
        if not self._started:
            raise RuntimeError("Call WavSource.start() before read().")
        if self._eof or self._stop.is_set():
            return None
        # The first block becomes available after its own capture duration.
        end_frame = min(self._frames_read + self.block_samples, self._total_frames)
        captured_at = self._started_at + end_frame / float(self.sample_rate)
        if self.realtime:
            remaining = captured_at - time.monotonic()
            if remaining > 0:
                if self._stop.wait(min(remaining, timeout)):
                    return None
                if time.monotonic() < captured_at:
                    return None
        with self._lock:
            if self._stop.is_set() or self._wave is None:
                return None
            raw = self._wave.readframes(self.block_samples)
            if not raw:
                self._eof = True
                return None
            sample_count = len(raw) // (2 * self._channels)
            self._frames_read += sample_count
            chunk = AudioChunk(_pcm16_to_float(raw, self._channels), self.sample_rate,
                               self._sequence, captured_at)
            self._sequence += 1
            self._stats["chunks_captured"] += 1
            self._stats["chunks_read"] += 1
            self._eof = self._frames_read >= self._total_frames
            return chunk

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self._wave is not None:
                self._wave.close()
                self._wave = None
            self._eof = True


class _QueuedPcmSource:
    """Shared bounded raw-PCM transport; producer work is deliberately small."""

    def __init__(self, sample_rate: int, block_ms: float, queue_blocks: int) -> None:
        self.sample_rate = TARGET_SAMPLE_RATE
        self.block_samples = _block_samples(sample_rate, block_ms)
        if isinstance(queue_blocks, bool) or int(queue_blocks) != queue_blocks or int(queue_blocks) < 1:
            raise ValueError("queue_blocks must be a positive integer.")
        self.queue_blocks = int(queue_blocks)
        self._queue = queue.Queue(maxsize=self.queue_blocks)  # type: queue.Queue
        self._stop = threading.Event()
        self._producer_done = threading.Event()
        self._stats_lock = threading.Lock()
        self._sequence = 0
        self._last_read_sequence = -1
        self._started = False
        self._stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats() -> Dict[str, Any]:
        return {"chunks_captured": 0, "chunks_read": 0, "dropped_blocks": 0,
                "discontinuities": 0, "errors": 0, "last_error": None,
                "capture_status_events": 0}

    @property
    def eof(self) -> bool:
        # Drain captured chunks before reporting end-of-stream to the worker.
        return self._producer_done.is_set() and self._queue.empty()

    @property
    def stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            result = dict(self._stats)
        result.update({"queue_size": self._queue.qsize(), "queue_capacity": self.queue_blocks})
        return result

    def _reset(self) -> None:
        self._stop.clear()
        self._producer_done.clear()
        self._drain()
        self._sequence = 0
        self._last_read_sequence = -1
        with self._stats_lock:
            self._stats = self._fresh_stats()
        self._started = True

    def _error(self, message: str) -> None:
        with self._stats_lock:
            self._stats["errors"] += 1
            self._stats["last_error"] = str(message)

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _enqueue(self, raw: bytes, captured_at: float, discontinuous: bool = False) -> None:
        if self._stop.is_set():
            return
        sequence = self._sequence
        self._sequence += 1
        dropped = 0
        while True:
            try:
                self._queue.put_nowait((sequence, captured_at, raw, discontinuous or dropped > 0))
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    dropped += 1
                except queue.Empty:
                    continue
        with self._stats_lock:
            self._stats["chunks_captured"] += 1
            self._stats["dropped_blocks"] += dropped

    def read(self, timeout: float = 0.05) -> Optional[AudioChunk]:
        timeout = _timeout(timeout)
        if not self._started:
            raise RuntimeError("Call audio source start() before read().")
        if self.eof or self._stop.is_set():
            return None
        try:
            sequence, captured_at, raw, marked = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        discontinuous = bool(marked or sequence != self._last_read_sequence + 1)
        self._last_read_sequence = sequence
        with self._stats_lock:
            self._stats["chunks_read"] += 1
            self._stats["discontinuities"] += int(discontinuous)
        return AudioChunk(_pcm16_to_float(raw), self.sample_rate, sequence,
                          float(captured_at), discontinuous)

    def _finish_stop(self) -> None:
        self._drain()
        self._producer_done.set()


class SoundDeviceSource(_QueuedPcmSource):
    """Optional PortAudio source.  No resampling or model work in its callback."""

    def __init__(self, device: Optional[Union[str, int]] = None,
                 sample_rate: int = TARGET_SAMPLE_RATE, block_ms: float = DEFAULT_BLOCK_MS,
                 queue_blocks: int = 4) -> None:
        super().__init__(sample_rate, block_ms, queue_blocks)
        self.device = device
        self._stream = None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("sounddevice is not installed; select ALSA or WAV, or install it explicitly.") from exc
        try:
            sd.check_input_settings(device=self.device, channels=1,
                                    dtype="int16", samplerate=self.sample_rate)
        except Exception as exc:
            raise RuntimeError(
                "Microphone cannot capture 16000 Hz mono PCM16 through PortAudio. "
                "On PYNQ use ALSA plughw (ALSA converts the hardware format), "
                "or convert a recording explicitly to 16000 Hz PCM16 WAV. " + str(exc)) from exc
        self._reset()
        try:
            self._stream = sd.RawInputStream(
                device=self.device, samplerate=self.sample_rate, channels=1,
                dtype="int16", blocksize=self.block_samples, callback=self._callback,
                finished_callback=self._producer_done.set)
            self._stream.start()
        except Exception as exc:
            self._error(str(exc))
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            self._producer_done.set()
            raise RuntimeError("Cannot start 16000 Hz PCM16 microphone capture: " + str(exc)) from exc

    def _callback(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        # bytes() owns the callback buffer after PortAudio reuses it.
        raw = bytes(indata)
        captured_at = time.monotonic()
        # PortAudio timestamps share a clock.  Offset their capture-end time
        # into monotonic time so driver buffering is included in latency.
        if time_info is not None:
            try:
                adc_time = float(time_info.inputBufferAdcTime)
                current_time = float(time_info.currentTime)
                age = current_time - (adc_time + frames / float(self.sample_rate))
                if adc_time > 0 and current_time > 0 and 0 <= age <= 5.0:
                    captured_at -= age
            except (AttributeError, TypeError, ValueError):
                pass
        discontinuous = bool(status)
        if status:
            with self._stats_lock:
                self._stats["capture_status_events"] += 1
                self._stats["last_error"] = str(status)
        if len(raw) != frames * 2:
            self._error("PortAudio returned an incomplete PCM16 mono block.")
            return
        self._enqueue(raw, captured_at, discontinuous)

    def stop(self) -> None:
        self._stop.set()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                # abort avoids waiting for queued audio buffers to play/capture.
                stream.abort()
            except Exception as exc:
                self._error("Microphone stop: " + str(exc))
            finally:
                try:
                    stream.close()
                except Exception as exc:
                    self._error("Microphone close: " + str(exc))
        self._finish_stop()


class AlsaSource(_QueuedPcmSource):
    """Linux arecord capture with a bounded reader queue and explicit PCM format.

    ``plughw`` lets ALSA negotiate/convert a 48 kHz-only USB receiver to the
    requested 16 kHz stream.  The voice model always receives actual 16 kHz PCM.
    """

    def __init__(self, device: str = "plughw:1,0", sample_rate: int = TARGET_SAMPLE_RATE,
                 block_ms: float = DEFAULT_BLOCK_MS, queue_blocks: int = 4,
                 period_time_us: Optional[int] = None, buffer_time_us: Optional[int] = None) -> None:
        super().__init__(sample_rate, block_ms, queue_blocks)
        self.device = str(device)
        self.period_time_us, self.buffer_time_us = period_time_us, buffer_time_us
        for value in (period_time_us, buffer_time_us):
            if value is not None and (int(value) != value or value <= 0):
                raise ValueError('ALSA period/buffer time must be a positive integer in microseconds')
        if period_time_us is not None and buffer_time_us is not None and buffer_time_us < period_time_us:
            raise ValueError('ALSA buffer must be at least one period')
        self._process = None  # type: Optional[subprocess.Popen]
        self._reader_thread = None  # type: Optional[threading.Thread]
        self._stderr_thread = None  # type: Optional[threading.Thread]
        self._stderr_tail = deque(maxlen=12)  # type: deque

    @property
    def stats(self) -> Dict[str, Any]:
        result = super().stats
        with self._stats_lock:
            result["stderr_tail"] = "".join(list(self._stderr_tail)).strip()
        process = self._process
        result["process_returncode"] = None if process is None else process.poll()
        result['timestamp_scope'] = 'PCM delivered by arecord; excludes unknown USB/ALSA capture buffering'
        result['requested_period_time_us'] = self.period_time_us
        result['requested_buffer_time_us'] = self.buffer_time_us
        return result

    def start(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            self.stop()
        self._reset()
        self._stderr_tail.clear()
        command = ["arecord", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                   "-r", str(self.sample_rate), "-c", "1"]
        if self.period_time_us is not None:
            command.append('--period-time=' + str(int(self.period_time_us)))
        if self.buffer_time_us is not None:
            command.append('--buffer-time=' + str(int(self.buffer_time_us)))
        try:
            self._process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, bufsize=0)
        except OSError as exc:
            self._error(str(exc))
            self._producer_done.set()
            raise RuntimeError("Cannot start ALSA arecord; install alsa-utils and check the capture device: " + str(exc)) from exc
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="voice-alsa-stderr", daemon=True)
        self._reader_thread = threading.Thread(target=self._reader, name="voice-alsa-pcm", daemon=True)
        self._stderr_thread.start()
        self._reader_thread.start()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while not self._stop.is_set():
                line = process.stderr.readline()
                if not line:
                    break
                with self._stats_lock:
                    self._stderr_tail.append(line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass

    def _reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._producer_done.set()
            return
        block_bytes = self.block_samples * 2
        try:
            while not self._stop.is_set():
                parts = bytearray()
                while len(parts) < block_bytes and not self._stop.is_set():
                    data = process.stdout.read(block_bytes - len(parts))
                    if not data:
                        break
                    parts.extend(data)
                if self._stop.is_set():
                    break
                if not parts:
                    break
                if len(parts) != block_bytes:
                    self._error("ALSA ended with an incomplete PCM16 capture block.")
                    break
                self._enqueue(bytes(parts), time.monotonic())
            if not self._stop.is_set():
                try:
                    returncode = process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    returncode = process.poll()
                stderr_thread = self._stderr_thread
                if stderr_thread is not None:
                    stderr_thread.join(timeout=0.25)
                message = "ALSA capture ended unexpectedly (return code {}).".format(returncode)
                with self._stats_lock:
                    detail = "".join(list(self._stderr_tail)).strip()
                self._error(message + (" " + detail if detail else ""))
        except (OSError, ValueError) as exc:
            if not self._stop.is_set():
                self._error("ALSA reader: " + str(exc))
        finally:
            self._producer_done.set()

    def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self._error("ALSA capture did not terminate after kill.")
            except OSError as exc:
                self._error("ALSA stop: " + str(exc))
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=0.5)
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass
        self._finish_stop()


def create_audio_source(config: Dict[str, Any], root: Union[str, Path]) -> Any:
    """Construct an unstarted source from ``audio_source`` configuration."""
    config = dict(config or {})
    kind = str(config.get("kind", "sounddevice")).lower()
    sample_rate = int(config.get("sample_rate", TARGET_SAMPLE_RATE))
    block_ms = float(config.get("block_ms", DEFAULT_BLOCK_MS))
    _block_samples(sample_rate, block_ms)
    if kind == "wav":
        if block_ms != DEFAULT_BLOCK_MS:
            raise ValueError("WAV streaming uses fixed 30 ms blocks.")
        if not config.get("path"):
            raise ValueError("WAV audio source requires a path.")
        path = Path(config["path"])
        if not path.is_absolute():
            path = Path(root) / path
        return WavSource(path, realtime=bool(config.get("realtime", True)))
    options = {"sample_rate": sample_rate, "block_ms": block_ms,
               "queue_blocks": config.get("queue_blocks", 4)}
    if kind == "sounddevice":
        return SoundDeviceSource(device=config.get("device"), **options)
    if kind == "alsa":
        return AlsaSource(device=config.get("device") or "plughw:1,0",
                          period_time_us=config.get('period_time_us'),
                          buffer_time_us=config.get('buffer_time_us'), **options)
    raise ValueError("Unknown audio source kind {!r}; choose wav, sounddevice or alsa.".format(kind))
