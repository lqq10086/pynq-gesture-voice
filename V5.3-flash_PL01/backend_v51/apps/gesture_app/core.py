"""Computer stand-ins for the future PYNQ PL and PS video pipeline.

Coordinates in this module always refer to the mirrored, unzoomed input frame.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import ClassVar

import cv2
import numpy as np


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    box: tuple[int, int, int, int]
    objectness: float | None = None
    class_probability: float | None = None

    @property
    def center(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, (y1 + y2) / 2


@dataclass
class PLFrame:
    image: np.ndarray
    luma: np.ndarray
    edges: np.ndarray


class SoftwarePL:
    """PL-shaped interface: mirrored BGR frame in, frame and diagnostics out.

    This is a software behavioral model, not an FPGA overlay or DMA driver.
    The image sent to YOLO stays unchanged to match the trained camera path.
    """

    def process(self, mirrored_bgr: np.ndarray) -> PLFrame:
        if mirrored_bgr.dtype != np.uint8 or mirrored_bgr.ndim != 3 or mirrored_bgr.shape[2] != 3:
            raise ValueError("PL input must be an HxWx3 uint8 BGR frame")
        image = np.ascontiguousarray(mirrored_bgr)
        luma = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(luma, 75, 150)
        return PLFrame(image=image, luma=luma, edges=edges)


@dataclass
class InteractionState:
    ZOOM_GRACE_S: ClassVar[float] = 1.5
    zoom: float = 1.0
    zoom_state: str = "IDLE"
    status: str = "Show two open palms; move apart or together to zoom"
    _zoom_start_distance: float | None = None
    _zoom_start_value: float = 1.0
    _zoom_last_pair_time: float | None = None
    _zoom_missing_pairs: int = 0
    _zoom_rebase_pending: bool = False

    def reset(self) -> None:
        self.__dict__.update(type(self)().__dict__)

    def end_zoom_session(self) -> None:
        """End palm tracking while retaining the current background scale."""
        self._zoom_start_distance = None
        self._zoom_last_pair_time = None
        self._zoom_missing_pairs = 0
        self._zoom_rebase_pending = False
        self.zoom_state = "IDLE"
        self.status = "Show two open palms; move apart or together to zoom"

    def expire_zoom_result(self) -> None:
        """Mark stale geometry without counting a missing detector observation."""
        if self._zoom_start_distance is not None:
            self.zoom_state = "TEMPORARY_LOST"

    def expire_zoom_session(self, now: float, trace=None) -> bool:
        """A render tick may end tracking, but never uses old palm positions."""
        if (self._zoom_last_pair_time is not None
                and now - self._zoom_last_pair_time > self.ZOOM_GRACE_S):
            self.end_zoom_session()
            if trace is not None:
                trace(dict(kind='palm_zoom', action='end', reason='pair_timeout',
                           monotonic=now, zoom=self.zoom))
            return True
        return False

    def update_detections(self, detections: list[Detection], trace=None, *, now=None) -> None:
        """Consume one fresh observation; callers own result age/order checks."""
        now = time.monotonic() if now is None else now
        self.expire_zoom_session(now, trace=trace)
        palms = sorted((d for d in detections if d.label == "open_palm"), key=lambda d: d.confidence, reverse=True)[:2]
        if len(palms) == 2:
            distance = math.dist(palms[0].center, palms[1].center)
            baseline = self._zoom_start_distance is None
            rebase = self._zoom_rebase_pending
            if trace is not None:
                zoom_before = self.zoom
            if baseline or rebase:
                self._zoom_start_distance = max(distance, 1)
                self._zoom_start_value = self.zoom
                target = self.zoom
            else:
                target = self._zoom_start_value * distance / self._zoom_start_distance
                # Fresh geometry sets the target; the display smooths it once.
                self.zoom = max(0.75, min(2.5, target))
            self._zoom_last_pair_time = now
            self._zoom_missing_pairs = 0
            self._zoom_rebase_pending = False
            self.zoom_state = "TWO_PALMS_DETECTED" if baseline or rebase else "ZOOM_ACTIVE"
            if trace is not None:
                trace(dict(kind='palm_zoom', action=('baseline' if baseline else
                           'rebase' if rebase else 'update'),
                           distance=distance, zoom_before=zoom_before, zoom_target=target,
                           zoom=self.zoom, zoom_state=self.zoom_state))
            self.status = "Two palms: move apart / together to zoom"
        else:
            if trace is not None:
                trace(dict(kind='palm_zoom', action='not_applied', reason='fewer_than_two_open_palms'))
            if self._zoom_start_distance is not None:
                self._zoom_missing_pairs += 1
                self._zoom_rebase_pending = True
                self.zoom_state = "TEMPORARY_LOST"
                if self._zoom_missing_pairs >= 2:
                    self.end_zoom_session()
            self.status = "Show two open palms; move apart or together to zoom"


class SoftwarePLCompositor:
    """Draw the processed output that a later PL display path can reproduce."""

    @staticmethod
    def _map(point: tuple[float, float], zoom: float, width: int, height: int) -> tuple[int, int]:
        return (round((point[0] - width / 2) * zoom + width / 2),
                round((point[1] - height / 2) * zoom + height / 2))

    def render(self, pl_frame: PLFrame, detections: list[Detection], state: InteractionState,
               show_edges: bool = False, show_boxes: bool = False) -> np.ndarray:
        source = pl_frame.image
        height, width = source.shape[:2]
        center = (width / 2, height / 2)
        matrix = cv2.getRotationMatrix2D(center, 0, state.zoom)
        output = cv2.warpAffine(source, matrix, (width, height), flags=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_CONSTANT, borderValue=(18, 18, 18))

        if show_boxes:
            palette = {"open_palm": (80, 220, 80), "side_palm": (255, 190, 70), "pinch_point": (220, 90, 255)}
            for detection in detections:
                x1, y1, x2, y2 = detection.box
                p1 = self._map((x1, y1), state.zoom, width, height)
                p2 = self._map((x2, y2), state.zoom, width, height)
                color = palette.get(detection.label, (255, 255, 255))
                cv2.rectangle(output, p1, p2, color, 2)
                cv2.putText(output, f"{detection.label} {detection.confidence:.2f}",
                            (p1[0], max(16, p1[1] - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.52, color, 2)
        if show_edges:
            small_w = max(1, width // 4)
            small_h = max(1, height // 4)
            inset = cv2.resize(pl_frame.edges, (small_w, small_h))
            inset = cv2.cvtColor(inset, cv2.COLOR_GRAY2BGR)
            output[height - small_h:height, width - small_w:width] = inset
        return output
