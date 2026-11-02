"""Explicit PYNQ video interfaces; no assumed board hierarchy or addresses.

Video configuration APIs must be selected explicitly: ``mode_attribute`` for
VDMA channels, ``video_in`` for VideoIn.configure(pixel_format), or
``video_out`` for VideoOut.configure(VideoMode, pixel_format). RGB565 means
uint16[H,W] words, R[15:11]/G[10:5]/B[4:0]; byte-packed frames are rejected.
PYNQ's standard 16-bit video mode alone does NOT implement RGB565 conversion.
"""
from pathlib import Path
import re

import numpy as np


def resolve_ip(overlay, path):
    if not isinstance(path, str) or not path or not all(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", part)
            for part in path.replace("/", ".").split(".")):
        raise ValueError("An explicit valid overlay IP/hierarchy path is required")
    result = overlay
    for part in path.replace("/", ".").split("."):
        try:
            result = getattr(result, part)
        except AttributeError as exc:
            raise ValueError("Overlay has no configured IP path: " + path) from exc
    return result


def load_overlay(config):
    if not config.get("bitfile"):
        raise ValueError("bitfile must name the actual board .bit artifact")
    bitfile = Path(config["bitfile"]).expanduser()
    if bitfile.suffix.lower() != ".bit":
        raise ValueError("bitfile must point to a .bit artifact with a matching .hwh")
    if not bitfile.is_file() or not bitfile.with_suffix(".hwh").is_file():
        raise FileNotFoundError("A real .bit and matching .hwh are required: " + str(bitfile))
    from pynq import Overlay
    return Overlay(str(bitfile), download=flag(config, "download", True))


def flag(config, name, default=False):
    value = config.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(name + " must be a bool")
    return value


def free_buffer(buffer):
    method = getattr(buffer, "freebuffer", None)
    if not callable(method):
        raise TypeError("The PYNQ frame/buffer must implement freebuffer()")
    method()


class PynqVideoBridge:
    """read_frame -> independent uint8 BGR; write_frame transfers ownership.

    Required config: reader_ip, writer_ip, width, height, pixel_format.
    bitfile is required unless the overlay or both channels are injected.
    configure_reader/writer and start_reader/writer default False. Channels
    already running are neither reconfigured nor stopped by this bridge.
    If running is not exposed, reader_already_started/writer_already_started
    must explicitly describe the state before configuration/start is requested.
    """
    FORMATS = {"RGB888": 24, "BGR888": 24, "RGB565": 16}

    def __init__(self, config, overlay=None, reader=None, writer=None):
        self.config = dict(config)
        self.width, self.height = config.get("width"), config.get("height")
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0
               for v in (self.width, self.height)):
            raise ValueError("width and height must be positive integers")
        self.pixel_format = config.get("pixel_format")
        if self.pixel_format not in self.FORMATS:
            raise ValueError("pixel_format must be RGB888, BGR888 or RGB565")
        for key in ("reader_ip", "writer_ip"):
            # Validate names even when mocks/channels are injected.
            path = config.get(key)
            if not isinstance(path, str) or not path:
                raise ValueError(key + " is required; no board IP names are guessed")
            if not all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", p)
                       for p in path.replace("/", ".").split(".")):
                raise ValueError("Invalid IP path in " + key)
        if overlay is None and (reader is None or writer is None):
            overlay = load_overlay(config)
        self.overlay = overlay
        self.reader = reader if reader is not None else resolve_ip(overlay, config["reader_ip"])
        self.writer = writer if writer is not None else resolve_ip(overlay, config["writer_ip"])
        for obj, methods in ((self.reader, ("readframe",)),
                             (self.writer, ("newframe", "writeframe"))):
            if not all(callable(getattr(obj, name, None)) for name in methods):
                raise TypeError("Configured video channel lacks " + ", ".join(methods))
        self._started = []
        self._configured = []
        self._uncertain_writes = []
        self._closed = False
        try:
            for name, channel in (("reader", self.reader), ("writer", self.writer)):
                configure = flag(config, "configure_" + name)
                start = flag(config, "start_" + name)
                if not (configure or start):
                    continue
                running = getattr(channel, "running", None)
                if running is None:
                    state_key = name + "_already_started"
                    if state_key not in config:
                        raise ValueError(state_key + " is required when channel has no running property")
                    running = flag(config, state_key)
                if bool(running):
                    continue
                if configure:
                    self._configure(name, channel)
                if start:
                    if not callable(getattr(channel, "start", None)) or not callable(getattr(channel, "stop", None)):
                        raise TypeError("Owned video lifecycle requires start()/stop()")
                    self._started.append(channel)
                    channel.start()
        except Exception:
            self.close()
            raise

    def _configure(self, name, channel):
        from pynq.lib.video import VideoMode
        api = self.config.get(name + "_configure_api")
        if api not in {"mode_attribute", "video_in", "video_out"}:
            raise ValueError(name + "_configure_api must explicitly select mode_attribute/video_in/video_out")
        fps = self.config.get("fps", 30)
        if not isinstance(fps, (float, int)) or isinstance(fps, bool) or not np.isfinite(fps) or fps <= 0:
            raise ValueError("fps must be positive and finite")
        mode = VideoMode(self.width, self.height, self.FORMATS[self.pixel_format], fps)
        if api == "mode_attribute":
            if not hasattr(channel, "mode"):
                raise TypeError("mode_attribute requires a writable channel.mode")
            channel.mode = mode
        else:
            if self.pixel_format == "RGB565":
                raise ValueError("Stock PYNQ VideoIn/VideoOut does not provide RGB565; use the actual custom uint16 pipeline")
            from pynq.lib.video import PIXEL_BGR, PIXEL_RGB
            pixel = PIXEL_RGB if self.pixel_format == "RGB888" else PIXEL_BGR
            if not callable(getattr(channel, "configure", None)):
                raise TypeError("Configured channel lacks configure()")
            if not callable(getattr(channel, "close", None)):
                raise TypeError("Owned VideoIn/VideoOut configuration requires close()")
            # These hierarchy configure() calls start their video frontend,
            # even when the optional VDMA start flag is False.
            self._configured.append(channel)
            if api == "video_in":
                channel.configure(pixelformat=pixel)
            else:
                channel.configure(mode, pixelformat=pixel)

    def _check_open(self):
        if self._closed:
            raise RuntimeError("Video bridge is closed")

    def _check_frame(self, frame):
        if not isinstance(frame, np.ndarray) or not callable(getattr(frame, "freebuffer", None)):
            raise TypeError("Video API must return a PynqBuffer-compatible numpy frame")
        expected = ((self.height, self.width) if self.pixel_format == "RGB565"
                    else (self.height, self.width, 3))
        dtype_ok = (frame.dtype.kind == "u" and frame.dtype.itemsize == 2
                    if self.pixel_format == "RGB565" else frame.dtype == np.uint8)
        if frame.shape != expected or not dtype_ok:
            raise ValueError("Unexpected video layout; " + self.pixel_format + " requires " + str(expected)
                             + (" uint16 words (packed uint8 byte order is not guessed)" if self.pixel_format == "RGB565" else " uint8"))

    def read_frame(self):
        self._check_open()
        frame = self.reader.readframe()
        try:
            self._check_frame(frame)
            invalidate = getattr(frame, "invalidate", None)
            if callable(invalidate):
                invalidate()
            if self.pixel_format == "RGB565":
                words = frame.astype(np.uint16, copy=False)
                r, g, b = (words >> 11) & 31, (words >> 5) & 63, words & 31
                return np.stack(((b << 3) | (b >> 2), (g << 2) | (g >> 4),
                                 (r << 3) | (r >> 2)), axis=-1).astype(np.uint8)
            source = frame[..., ::-1] if self.pixel_format == "RGB888" else frame
            return np.array(source, dtype=np.uint8, copy=True, order="C")
        finally:
            free_buffer(frame)

    def write_frame(self, bgr):
        self._check_open()
        if not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8 or bgr.shape != (self.height, self.width, 3):
            raise ValueError("write_frame requires uint8 HWC BGR at the configured dimensions")
        frame = self.writer.newframe()
        submitted = False
        try:
            self._check_frame(frame)
            if self.pixel_format == "RGB565":
                pixels = bgr.astype(np.uint16)
                frame[:] = ((pixels[..., 2] >> 3) << 11) | ((pixels[..., 1] >> 2) << 5) | (pixels[..., 0] >> 3)
            else:
                frame[:] = bgr[..., ::-1] if self.pixel_format == "RGB888" else bgr
            flush = getattr(frame, "flush", None)
            if callable(flush):
                flush()
            submitted = True
            self.writer.writeframe(frame)
            # Successful writeframe owns the buffer; never free or touch it.
        except Exception:
            if submitted:
                # An exception can occur after the driver took ownership.
                # stop() does not prove ownership: the driver may free it.
                # Retain the reference without a second explicit free.
                self._uncertain_writes.append(frame)
            else:
                free_buffer(frame)
            raise

    def close(self):
        if self._closed:
            return
        errors = []
        while self._started:
            channel = self._started.pop()
            try:
                channel.stop()
            except Exception as exc:
                self._started.append(channel)
                errors.append(exc)
                break
        if not errors:
            while self._configured:
                channel = self._configured.pop()
                try:
                    channel.close()
                except Exception as exc:
                    self._configured.append(channel)
                    errors.append(exc)
                    break
        if self._uncertain_writes:
            errors.append(RuntimeError("Failed writeframe ownership is uncertain; frames are retained without manual free, even after an owned writer stops"))
        if errors:
            raise RuntimeError("Video bridge cleanup incomplete: " + str(errors[0])) from errors[0]
        self._closed = True
