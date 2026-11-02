"""PC/V4L2 frame sources and optional recording sinks."""
from __future__ import annotations
import math
from pathlib import Path
import platform
import shutil
import tempfile
import time

import cv2
import numpy as np


def read_image(path):
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Cannot decode image: %s" % path)
    return image


def save_image(path, image):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(path.suffix, image)
    if not ok:
        raise RuntimeError("Cannot encode output image")
    encoded.tofile(path)


class OpenCvSource:
    def __init__(self, config, root):
        value = str(config.get("source", "0"))
        self.camera = value.isdecimal() or value.startswith("/dev/video")
        source = int(value) if value.isdecimal() else value
        if not self.camera:
            path = Path(source)
            source = str(path if path.is_absolute() else Path(root) / path)
        self.image_source = not self.camera and Path(source).suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")
        self.capture, self.image = None, None
        self.fps = 30.0
        if self.image_source:
            self.image = read_image(source)
        else:
            backend = cv2.CAP_DSHOW if self.camera and platform.system() == "Windows" else cv2.CAP_ANY
            self.capture = cv2.VideoCapture(source, backend)
            if not self.capture.isOpened() and backend != cv2.CAP_ANY:
                self.capture.release()
                self.capture = cv2.VideoCapture(source)
            if not self.capture.isOpened():
                self.capture.release()
                raise RuntimeError("Cannot open video source: %s" % source)
            if self.camera:
                self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, config.get("width", 1280))
                self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, config.get("height", 720))
                self.capture.set(cv2.CAP_PROP_FPS, config.get("fps", 30))
            fps = self.capture.get(cv2.CAP_PROP_FPS)
            if math.isfinite(fps) and fps >= 1:
                self.fps = fps

    def read_frame(self):
        if self.image_source:
            image, self.image = self.image, None
            return image
        ok, frame = self.capture.read()
        return frame if ok else None

    def close(self):
        if self.capture is not None:
            self.capture.release()


class FileSink:
    def __init__(self, path, image_source=False, fps=30, wall_time=False):
        self.path = Path(path)
        self.image_source, self.fps, self.wall_time = image_source, fps, wall_time
        self.writer, self.temp_path = None, None
        self.start = None
        self.frames = 0
        self.shape = None
        if image_source and self.path.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("Image output must be .png/.jpg/.jpeg")
        if not image_source and self.path.suffix.lower() != ".avi":
            raise ValueError("Video output must be .avi (MJPG)")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write_frame(self, image):
        if self.image_source:
            save_image(self.path, image)
            self.frames += 1
            return
        height, width = image.shape[:2]
        if self.writer is None:
            self.temp_path = Path(tempfile.gettempdir()) / ("pynq_gesture_%s.avi" % time.time_ns())
            self.writer = cv2.VideoWriter(str(self.temp_path), cv2.VideoWriter_fourcc(*"MJPG"), self.fps, (width, height))
            if not self.writer.isOpened():
                raise RuntimeError("Cannot initialize MJPG video writer")
            self.start = time.perf_counter()
            self.shape = image.shape
        if image.shape != self.shape:
            raise ValueError("Recording requires constant frame dimensions")
        target = max(self.frames + 1, round((time.perf_counter() - self.start) * self.fps)) if self.wall_time else self.frames + 1
        while self.frames < target:
            self.writer.write(image)
            self.frames += 1

    def close(self):
        if self.writer is not None:
            self.writer.release()
        if self.temp_path is not None and self.temp_path.exists():
            shutil.move(str(self.temp_path), str(self.path))
