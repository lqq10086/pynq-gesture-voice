"""Training-compatible MFCC frontend without a mandatory SciPy dependency.

The supplied English model takes mono float32 audio at 16 kHz, fixed to
24,000 samples, and float32 NCHW features of shape [1, 1, 40, 151].
This module deliberately follows the supplied training implementation, including
its preemphasis, reflect centering, periodic Hann window and natural logarithm.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

import numpy as np


def fix_length(audio: np.ndarray, num_samples: int = 24000,
               pad_mode: str = "zero") -> np.ndarray:
    """Center crop long utterances; append zeroes to short ones, as in training."""
    num_samples = int(num_samples)
    if num_samples <= 0:
        raise ValueError("num_samples must be positive")
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim != 1:
        raise ValueError("audio must be a one-dimensional mono waveform")
    if not np.isfinite(audio).all():
        raise ValueError("audio contains non-finite samples")
    n = int(audio.size)
    if n >= num_samples:
        start = (n - num_samples) // 2
        return np.ascontiguousarray(audio[start:start + num_samples])
    if pad_mode == "wrap" and n:
        repeats = int(np.ceil(float(num_samples) / n))
        return np.ascontiguousarray(np.tile(audio, repeats)[:num_samples])
    if pad_mode not in ("zero", "wrap"):
        raise ValueError("pad_mode must be 'zero' or 'wrap'")
    return np.ascontiguousarray(np.pad(audio, (0, num_samples - n)))


def load_wav(path: Union[str, Path], target_sr: Optional[int] = 16000
             ) -> Tuple[np.ndarray, int]:
    """Read uncompressed PCM16 WAV, averaging channels to mono.

    Sample-rate conversion belongs to the audio capture adapter. A differing
    sample rate is rejected rather than silently changing the MFCC contract;
    use target_sr=None to read the native waveform before explicit resampling.
    No WAV serialization is needed when the capture adapter supplies PCM arrays.
    """
    with wave.open(str(path), "rb") as stream:
        if stream.getcomptype() != "NONE" or stream.getsampwidth() != 2:
            raise ValueError("load_wav requires uncompressed 16-bit PCM WAV")
        sample_rate = int(stream.getframerate())
        channels = int(stream.getnchannels())
        raw = stream.readframes(stream.getnframes())
    if channels < 1:
        raise ValueError("WAV must contain at least one audio channel")
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels != 1:
        # Match the training reader's float64 channel average before conversion.
        audio = audio.reshape(-1, channels).astype(np.float64).mean(axis=1)
    if target_sr is not None and sample_rate != int(target_sr):
        raise ValueError("WAV sample rate is {} Hz; resample explicitly to {} Hz "
                         "in the audio adapter".format(sample_rate, target_sr))
    return np.ascontiguousarray(audio, dtype=np.float32), sample_rate


class MfccFrontend:
    """Build the exact model input, with NumPy FFT by default.

    ``config`` can be the complete JSON config or its ``mfcc`` section.
    ``fft_backend='scipy'`` is an explicit optional float32 FFT fast path;
    ``'auto'`` uses it if available. The NumPy path requires no SciPy package.
    """

    def __init__(self, config: Dict[str, Any], mean: Optional[np.ndarray],
                 std: Optional[np.ndarray], fft_backend: str = "numpy") -> None:
        full_config = dict(config)
        cfg = dict(full_config.get("mfcc", full_config))
        self.num_samples = int(full_config.get("audio", {}).get("num_samples", 24000))
        self.sample_rate = int(cfg.get("sample_rate", 16000))
        self.frame_length = int(round(float(cfg.get("frame_length_ms", 25.0))
                                      * self.sample_rate / 1000.0))
        self.win_length = int(cfg.get("win_length", self.frame_length))
        self.hop_length = int(cfg.get("hop_length", round(
            float(cfg.get("frame_shift_ms", 10.0)) * self.sample_rate / 1000.0)))
        self.n_fft = int(cfg.get("n_fft", 512))
        self.num_mel_bins = int(cfg.get("num_mel_bins", 40))
        self.num_ceps = int(cfg.get("num_ceps", 40))
        self.fmin = float(cfg.get("fmin", 20.0))
        self.fmax = float(cfg.get("fmax", self.sample_rate / 2.0))
        self.preemph = float(cfg.get("preemphasis", 0.97))
        self.center = bool(cfg.get("center", True))
        self.log_eps = float(cfg.get("log_eps", 1e-6))
        self.feature_type = str(cfg.get("feature_type", "mfcc")).lower()
        if self.feature_type != "mfcc":
            raise ValueError("the supplied English model requires MFCC features")
        if (self.frame_length != self.win_length or self.n_fft < self.win_length
                or self.hop_length <= 0 or self.num_samples <= 0
                or not (0.0 <= self.fmin < self.fmax <= self.sample_rate / 2.0)
                or self.num_mel_bins <= 0 or self.num_ceps <= 0 or self.log_eps <= 0):
            raise ValueError("invalid MFCC configuration")
        window_name = str(cfg.get("window", "hann")).lower()
        if window_name not in ("hann", "hanning"):
            raise ValueError("the supplied model requires the periodic Hann window")
        self.window = np.hanning(self.win_length + 1)[:-1].astype(np.float32)
        self.mel_fb = self._mel_filterbank()
        n = np.arange(self.num_mel_bins, dtype=np.float64)
        k = np.arange(self.num_ceps, dtype=np.float64)[:, None]
        dct = np.cos(np.pi * k * (2.0 * n[None, :] + 1.0)
                     / (2.0 * self.num_mel_bins))
        dct *= np.sqrt(2.0 / self.num_mel_bins)
        dct[0] *= np.sqrt(0.5)
        self.dct_matrix = dct.astype(np.float32)
        self.mean = None if mean is None else np.asarray(mean, dtype=np.float32).reshape(-1)
        self.std = None if std is None else np.asarray(std, dtype=np.float32).reshape(-1)
        if (self.mean is None) != (self.std is None):
            raise ValueError("both normalization mean and standard deviation are required")
        if self.mean is not None:
            if (self.mean.size != self.num_ceps or self.std.size != self.num_ceps
                    or not np.isfinite(self.mean).all() or not np.isfinite(self.std).all()
                    or np.any(self.std <= 0)):
                raise ValueError("invalid MFCC normalization statistics")
        elif bool(cfg.get("normalize", {}).get("enabled", False)):
            raise ValueError("normalization is enabled but its statistics are missing")
        if fft_backend not in ("numpy", "scipy", "auto"):
            raise ValueError("fft_backend must be numpy, scipy or auto")
        self.fft_backend = "numpy"
        self._rfft = np.fft.rfft
        if fft_backend != "numpy":
            try:
                from scipy.fft import rfft
                self._rfft = rfft
                self.fft_backend = "scipy"
            except ImportError:
                if fft_backend == "scipy":
                    raise ImportError("optional SciPy FFT requested but SciPy is not installed")
        pad = self.frame_length // 2 if self.center else 0
        self.num_frames = 1 + (self.num_samples + 2 * pad - self.frame_length) // self.hop_length
        if self.num_frames <= 0:
            raise ValueError("audio length is shorter than one analysis frame")
        self.input_shape = (1, 1, self.num_ceps, self.num_frames)
        self._indices = (np.arange(self.frame_length)[None, :]
                         + self.hop_length * np.arange(self.num_frames)[:, None])

    def _mel_filterbank(self) -> np.ndarray:
        fft_freqs = np.linspace(0.0, self.sample_rate / 2.0, self.n_fft // 2 + 1)
        mel_min = 2595.0 * np.log10(1.0 + self.fmin / 700.0)
        mel_max = 2595.0 * np.log10(1.0 + self.fmax / 700.0)
        mel_pts = np.linspace(mel_min, mel_max, self.num_mel_bins + 2)
        hz_pts = 700.0 * (10.0 ** (mel_pts / 2595.0) - 1.0)
        bank = np.zeros((self.num_mel_bins, self.n_fft // 2 + 1), dtype=np.float32)
        for index in range(self.num_mel_bins):
            left, middle, right = hz_pts[index:index + 3]
            rising = (fft_freqs - left) / max(middle - left, 1e-12)
            falling = (right - fft_freqs) / max(right - middle, 1e-12)
            bank[index] = (np.maximum(0.0, np.minimum(rising, falling))
                           * (2.0 / max(right - left, 1e-12)))
        return bank

    def fix_length(self, audio: np.ndarray) -> np.ndarray:
        return fix_length(audio, self.num_samples)

    def features(self, audio: np.ndarray) -> np.ndarray:
        waveform = self.fix_length(audio)
        if self.preemph > 0.0:
            emphasized = np.empty_like(waveform)
            emphasized[0] = waveform[0]
            emphasized[1:] = waveform[1:] - self.preemph * waveform[:-1]
            waveform = emphasized
        if self.center:
            padding = self.frame_length // 2
            waveform = np.pad(waveform, (padding, padding), mode="reflect")
        frames = waveform[self._indices] * self.window
        spectrum = self._rfft(frames, n=self.n_fft, axis=-1)
        power = (spectrum.real ** 2 + spectrum.imag ** 2).astype(np.float32)
        mel = power @ self.mel_fb.T
        log_mel = np.log(np.maximum(mel, self.log_eps)).astype(np.float32)
        feats = log_mel @ self.dct_matrix.T
        if self.mean is not None:
            feats = (feats - self.mean) / self.std
        return np.ascontiguousarray(feats.T[None, None, :, :], dtype=np.float32)


__all__ = ["MfccFrontend", "fix_length", "load_wav"]
