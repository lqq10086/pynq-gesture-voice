"""Uniform floating-window geometry and two-pinch corner resizing.

Game logic keeps its original image coordinates. Only the drawing difference
inside a rounded window is moved and resized, so the camera remains unchanged.
"""

from __future__ import annotations

from typing import Tuple
from functools import lru_cache

import itertools
import math
import platform

import cv2
import numpy as np
from frame_color import composite_window_bgr

from .spatial_ui import _rounded_mask, unit


Point = Tuple[float, float]

# The board's OpenCV 4.5.4 fixed-point sampler is verified byte-for-byte.
# Other implementations (including desktop OpenCV 5) retain warpAffine.
_ROI_WARP_EXACT = platform.machine() == 'armv7l' and cv2.__version__ == '4.5.4'


@lru_cache(maxsize=16)
def _outside_window(width, height, radius):
    """Cache only rounded geometry; every composited camera pixel stays fresh."""
    outside = _rounded_mask(width, height, radius) == 0
    outside.flags.writeable = False
    return outside


class WindowView:
    """A translation and uniform scale of one original game/window rectangle."""

    def __init__(self, base_rect: tuple[int, int, int, int], width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError("Frame dimensions must be positive")
        self.width, self.height = int(width), int(height)
        x1, y1, x2, y2 = base_rect
        self.base_rect = (max(0, int(x1)), max(0, int(y1)),
                          min(self.width, int(x2)), min(self.height, int(y2)))
        x1, y1, x2, y2 = self.base_rect
        if x2 <= x1 or y2 <= y1:
            raise ValueError("The base window must have positive area inside the frame")
        self._base_center = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
        self._base_size = (float(x2 - x1), float(y2 - y1))
        self.center = self._base_center
        self.scale = 1.0
        self.corner_radius = max(1, round(26 * unit(width, height)))
        self.margin = max(3.0, min(18.0, 0.014 * width, 0.018 * height))
        self._bottom = 0.87 * height - self.margin
        available_width = max(1.0, width - 2 * self.margin)
        available_height = max(1.0, self._bottom - self.margin)
        self.max_scale = min(available_width / self._base_size[0],
                             available_height / self._base_size[1])
        requested_minimum = min(1.0, max(0.5, 180.0 / self._base_size[0],
                                       100.0 / self._base_size[1]))
        self.min_scale = min(requested_minimum, self.max_scale)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        half_width = self._base_size[0] * self.scale / 2.0
        half_height = self._base_size[1] * self.scale / 2.0
        return (self.center[0] - half_width, self.center[1] - half_height,
                self.center[0] + half_width, self.center[1] + half_height)

    def set_geometry(self, center: Point, scale: float) -> None:
        if not all(math.isfinite(value) for value in (*center, scale)):
            raise ValueError("Window geometry must be finite")
        self.scale = max(self.min_scale, min(self.max_scale, float(scale)))
        half_width = self._base_size[0] * self.scale / 2.0
        half_height = self._base_size[1] * self.scale / 2.0
        self.center = (max(self.margin + half_width,
                           min(self.width - self.margin - half_width, float(center[0]))),
                       max(self.margin + half_height,
                           min(self._bottom - half_height, float(center[1]))))

    def to_screen(self, point: Point) -> Point:
        return (self.center[0] + (point[0] - self._base_center[0]) * self.scale,
                self.center[1] + (point[1] - self._base_center[1]) * self.scale)

    def to_local(self, point: Point) -> Point:
        return (self._base_center[0] + (point[0] - self.center[0]) / self.scale,
                self._base_center[1] + (point[1] - self.center[1]) / self.scale)

    def map_box_to_local(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        first, second = self.to_local((box[0], box[1])), self.to_local((box[2], box[3]))
        return (*first, *second)

    def _check_image(self, image: np.ndarray) -> None:
        if image.dtype != np.uint8 or image.shape != (self.height, self.width, 3):
            raise ValueError("Window images must match the frame's HxWx3 uint8 BGR shape")

    def virtual_scene(self, image: np.ndarray, destination=None, *, roi_only=False) -> np.ndarray:
        """Sample game coordinates; roi_only may leave workspace outside base_rect stale."""
        self._check_image(image)
        if destination is not None:
            self._check_image(destination)
            if not destination.flags.writeable or np.shares_memory(image, destination):
                raise ValueError('Virtual workspace must be private and writable')
        if roi_only and destination is not None and _ROI_WARP_EXACT:
            return self._virtual_scene_roi(image, destination)
        if self.scale == 1.0 and self.center == self._base_center:
            if destination is not None:
                np.copyto(destination, image)
                return destination
            return image.copy()
        matrix = np.array(((self.scale, 0.0, self.center[0] - self.scale * self._base_center[0]),
                           (0.0, self.scale, self.center[1] - self.scale * self._base_center[1])),
                          dtype=np.float32)
        return cv2.warpAffine(image, matrix, (self.width, self.height),
                              dst=destination,
                              flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                              borderMode=cv2.BORDER_REPLICATE)

    def _virtual_scene_roi(self, image, destination):
        x1, y1, x2, y2 = self.base_rect
        if self.scale == 1.0 and self.center == self._base_center:
            np.copyto(destination[y1:y2, x1:x2], image[y1:y2, x1:x2])
            return destination
        matrix = np.array(((self.scale, self.center[0] - self.scale * self._base_center[0]),
                           (self.scale, self.center[1] - self.scale * self._base_center[1])),
                          dtype=np.float32).astype(np.float64)
        key = (self.base_rect, *matrix.flat)
        if key != getattr(self, '_roi_map_key', None):
            # Match warpAffine's separate global-coordinate rounding, AB_BITS=10
            # and INTER_BITS=5. Cropping the matrix first shifts rounding ties.
            xs = np.arange(x1, x2, dtype=np.float64)
            ys = np.arange(y1, y2, dtype=np.float64)
            qx = ((np.rint(matrix[0, 0] * xs * 1024).astype(np.int32)
                   + int(np.rint(matrix[0, 1] * 1024)) + 16) >> 5)
            qy = ((np.rint((matrix[1, 0] * ys + matrix[1, 1]) * 1024)
                   .astype(np.int32) + 16) >> 5)
            xy = np.empty((y2-y1, x2-x1, 2), dtype=np.int16)
            xy[:, :, 0] = np.clip(qx >> 5, -32768, 32767)[None, :]
            xy[:, :, 1] = np.clip(qy >> 5, -32768, 32767)[:, None]
            alpha = (((qy[:, None] & 31) << 5) | (qx[None, :] & 31)).astype(np.uint16)
            self._roi_map_key, self._roi_maps = key, (xy, alpha)
        cv2.remap(image, *self._roi_maps, interpolation=cv2.INTER_LINEAR,
                  dst=destination[y1:y2, x1:x2], borderMode=cv2.BORDER_REPLICATE)
        return destination

    def composite(self, image: np.ndarray, virtual_before: np.ndarray,
                  virtual_after: np.ndarray) -> None:
        """Apply only the rounded window's drawing change to the real camera."""
        for frame in (image, virtual_after):
            self._check_image(frame)
        x1, y1, x2, y2 = self.base_rect
        before = virtual_before
        if before.shape == image.shape:
            before = before[y1:y2, x1:x2]
        if before.dtype != np.uint8 or before.shape != (y2-y1, x2-x1, 3):
            raise ValueError('Window before image must match the full frame or base window ROI')
        left, top, right, bottom = (round(value) for value in self.rect)
        target_width, target_height = right - left, bottom - top
        if target_width <= 0 or target_height <= 0:
            return
        clipped_left, clipped_top = max(0, left), max(0, top)
        clipped_right, clipped_bottom = min(self.width, right), min(self.height, bottom)
        if clipped_right <= clipped_left or clipped_bottom <= clipped_top:
            return
        source_outside = _outside_window(x2-x1,y2-y1,self.corner_radius)
        target_outside = _outside_window(target_width,target_height,max(1,round(self.corner_radius*self.scale)))
        destination = image[clipped_top:clipped_bottom, clipped_left:clipped_right]
        if composite_window_bgr(destination,before,virtual_after[y1:y2,x1:x2],source_outside,target_outside,
                                clipped_left-left,clipped_top-top):
            return
        difference = cv2.subtract(virtual_after[y1:y2, x1:x2], before, dtype=cv2.CV_32F)
        difference[source_outside] = 0.0
        if difference.shape[:2] != (target_height, target_width):
            difference = cv2.resize(difference, (target_width, target_height), interpolation=cv2.INTER_LINEAR)
        difference[target_outside] = 0.0
        delta = difference[clipped_top - top:clipped_bottom - top,
                           clipped_left - left:clipped_right - left]
        # Keep float addition before rounding: converting the delta first changes
        # half-way rounding when the destination byte has odd parity.
        combined = cv2.add(destination, delta, dtype=cv2.CV_32F)
        cv2.max(combined, 0, dst=combined)
        cv2.convertScaleAbs(combined, dst=destination)


class CornerResize:
    """Capture lower-left/upper-right pinches, then uniformly resize and move."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.active = False
        self.engaged = False
        self.points: tuple[Point, Point] | None = None
        self._last_pair: tuple[Point, Point] | None = None
        self._start_center: Point = (0.0, 0.0)
        self._start_midpoint: Point = (0.0, 0.0)
        self._start_scale = 1.0
        self._start_distance = 1.0
        self._axis: Point = (1.0, 0.0)

    @staticmethod
    def handle_radius(view: WindowView) -> float:
        x1, y1, x2, y2 = view.rect
        return max(20.0, min(66.0, 0.15 * min(x2 - x1, y2 - y1)))

    @staticmethod
    def _pair(points: list[Point], reference: tuple[Point, Point]) -> tuple[Point, Point]:
        # Test assignments rather than trusting the order in YOLO's detections.
        return min(itertools.permutations(points, 2),
                   key=lambda pair: math.dist(pair[0], reference[0]) ** 2
                   + math.dist(pair[1], reference[1]) ** 2)

    def update(self, points: list[Point], view: WindowView, trace=None) -> bool:
        """Return whether this observation must suppress normal pinch clicks.

        Any simultaneous pair consumes the result; a corner pair captures
        immediately. One incomplete fresh result releases resize and is also
        consumed so its remaining pinch cannot become a click.
        """
        points = [(float(x), float(y)) for x, y in points
                  if math.isfinite(x) and math.isfinite(y)]
        if len(points) < 2:
            self.points = None
            if self.engaged or self.active:
                if trace is not None:
                    trace(dict(kind='window_resize', action='release', pinch_count=len(points)))
                self.reset()
                return True
            return False

        x1, y1, x2, y2 = view.rect
        corners = ((x1, y2), (x2, y1))
        reference = self._last_pair if self._last_pair is not None else corners
        pair = self._pair(points, reference)

        if self.active:
            self.points = pair
            self._last_pair = pair
            midpoint = ((pair[0][0] + pair[1][0]) / 2.0,
                        (pair[0][1] + pair[1][1]) / 2.0)
            separation = (pair[1][0] - pair[0][0], pair[1][1] - pair[0][1])
            projection = separation[0] * self._axis[0] + separation[1] * self._axis[1]
            # An inverted/coincident diagonal cannot rotate or flip the window.
            if projection > max(8.0, self._start_distance * 0.03):
                center = (self._start_center[0] + midpoint[0] - self._start_midpoint[0],
                          self._start_center[1] + midpoint[1] - self._start_midpoint[1])
                view.set_geometry(center, self._start_scale * projection / self._start_distance)
                if trace is not None:
                    trace(dict(kind='window_resize', action='update', center=view.center,
                               scale=view.scale, rect=view.rect, points=pair))
            elif trace is not None:
                trace(dict(kind='window_resize', action='not_applied',
                           reason='inverted_or_coincident_diagonal', projection=projection))
            return True

        # Before capture, each hand must still be close to its intended corner.
        pair = self._pair(points, corners)
        radius = self.handle_radius(view)
        if any(math.dist(point, corner) > radius for point, corner in zip(pair, corners)):
            if trace is not None:
                trace(dict(kind='window_resize', action='not_applied',
                           reason='pinches_not_at_handles', points=pair,
                           corners=corners, handle_radius=radius))
            self.engaged = False
            self.points = None
            self._last_pair = None
            return True
        self.engaged = True
        self.points = pair
        self._last_pair = pair
        dx, dy = pair[1][0] - pair[0][0], pair[1][1] - pair[0][1]
        distance = math.hypot(dx, dy)
        if distance < 16.0:
            if trace is not None:
                trace(dict(kind='window_resize', action='not_applied',
                           reason='corner_distance_too_small', distance=distance))
            return True
        self.active = True
        self._start_center = view.center
        self._start_midpoint = ((pair[0][0] + pair[1][0]) / 2.0,
                                (pair[0][1] + pair[1][1]) / 2.0)
        self._start_scale = view.scale
        self._start_distance = distance
        self._axis = (dx / distance, dy / distance)
        if trace is not None:
            trace(dict(kind='window_resize', action='capture', center=view.center,
                       scale=view.scale, rect=view.rect, points=pair))
        return True
