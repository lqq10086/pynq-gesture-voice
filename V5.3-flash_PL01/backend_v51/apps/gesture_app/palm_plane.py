"""Estimate a finite side-palm contact line in the mirrored camera image.

YOLO provides an axis-aligned box, not joints or depth. A reliable skin region
can refine that box's long axis; otherwise the box supplies the orientation.
This is a two-dimensional approximation of the visible edge of the hand.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .core import Detection


@dataclass(frozen=True)
class SidePalmPlane:
    center: tuple[float, float]
    tangent: tuple[float, float]
    half_length: float
    estimated: bool


def _skin_axis(roi: np.ndarray, box_axis: np.ndarray,
               box_aspect: float) -> tuple[np.ndarray, float] | None:
    """Return a dominant skin axis and its length, or reject ambiguous ROIs."""
    source_height, source_width = roi.shape[:2]
    scale = min(1.0, 256.0 / max(source_height, source_width))
    if scale < 1.0:
        roi = cv2.resize(roi, (max(1, round(source_width * scale)),
                               max(1, round(source_height * scale))),
                         interpolation=cv2.INTER_AREA)
    height, width = roi.shape[:2]
    if min(height, width) < 8:
        return None

    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    # Chrominance plus saturation excludes grey walls and most clothing. These
    # intentionally broad bounds still require a central, elongated component.
    skin = cv2.inRange(ycrcb, (30, 128, 65), (255, 190, 145))
    colored = ((hsv[:, :, 1] >= 25) & (hsv[:, :, 2] >= 32)
               & ((hsv[:, :, 0] <= 28) | (hsv[:, :, 0] >= 170)))
    skin[~colored] = 0
    kernel = np.ones((3, 3), dtype=np.uint8)
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, kernel)
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, kernel)

    count, components, stats, _ = cv2.connectedComponentsWithStats(skin, 8)
    central = components[height // 4: max(height // 4 + 1, 3 * height // 4),
                         width // 4: max(width // 4 + 1, 3 * width // 4)]
    central_counts = np.bincount(central.ravel(), minlength=count)
    candidates = []
    for component in range(1, count):
        area = int(stats[component, cv2.CC_STAT_AREA])
        coverage = area / (height * width)
        # An almost uniformly skin-colored box is not evidence of a hand axis.
        if 0.06 <= coverage <= 0.88 and central_counts[component] >= 0.015 * height * width:
            candidates.append((area + 2 * int(central_counts[component]), component))
    if not candidates:
        return None
    component = max(candidates)[1]
    rows, columns = np.nonzero(components == component)
    points = np.column_stack((columns, rows)).astype(np.float64)
    # Undo resize separately per axis to avoid a small anisotropy from rounding.
    points[:, 0] *= source_width / width
    points[:, 1] *= source_height / height
    eigenvalues, eigenvectors = np.linalg.eigh(np.cov(points, rowvar=False))
    if eigenvalues[1] < 16 or eigenvalues[1] / max(eigenvalues[0], 1.0) < 1.8:
        return None
    tangent = eigenvectors[:, 1]
    # A strongly tall/wide YOLO box should not suddenly produce the other axis.
    if box_aspect >= 1.3 and abs(float(tangent @ box_axis)) < 0.70:
        return None
    if float(tangent @ box_axis) < 0:
        tangent = -tangent
    projection = points @ tangent
    span = float(np.percentile(projection, 90) - np.percentile(projection, 10))
    return tangent, span


def estimate_side_plane(detection: Detection, width: int, height: int,
                        image: np.ndarray | None = None) -> SidePalmPlane:
    """Get the palm line in pixels; ``image`` must be the unzoomed BGR input.

    The line stays centered inside the clipped detection box. Invalid/empty
    images, low skin coverage and ambiguous shapes all use the box's long axis.
    Frame dimensions must be positive; an empty detection returns zero length.
    """
    if width <= 0 or height <= 0:
        raise ValueError("Frame dimensions must be positive")
    x1, y1, x2, y2 = detection.box
    x1, x2 = min(max(float(x1), 0.0), width), min(max(float(x2), 0.0), width)
    y1, y2 = min(max(float(y1), 0.0), height), min(max(float(y2), 0.0), height)
    box_width, box_height = max(0.0, x2 - x1), max(0.0, y2 - y1)
    center = ((x1 + x2) / 2, (y1 + y2) / 2)
    tangent = np.array((1.0, 0.0) if box_width >= box_height else (0.0, 1.0))
    long_side = max(box_width, box_height)
    half_length = 0.42 * long_side if min(box_width, box_height) > 0 else 0.0
    fallback = SidePalmPlane(center, (float(tangent[0]), float(tangent[1])), half_length, False)
    if (half_length == 0 or image is None or image.dtype != np.uint8
            or image.ndim != 3 or image.shape[2] != 3
            or image.shape[0] != height or image.shape[1] != width):
        return fallback

    roi = image[int(y1): int(y2), int(x1): int(x2)]
    if roi.size == 0:
        return fallback
    result = _skin_axis(roi, tangent, long_side / max(min(box_width, box_height), 1.0))
    if result is None:
        return fallback
    tangent, span = result
    # Keep the finite line near the hand and within the detection rectangle.
    half_length = min(0.44 * long_side, max(0.26 * long_side, 0.52 * span))
    for component, extent in zip(tangent, (box_width, box_height)):
        if abs(component) > 1e-6:
            half_length = min(half_length, 0.46 * extent / abs(float(component)))
    return SidePalmPlane(center, (float(tangent[0]), float(tangent[1])), float(half_length), True)
