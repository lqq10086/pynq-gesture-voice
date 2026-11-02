"""Rounded, translucent UI primitives for the camera-first spatial view."""

from __future__ import annotations

from functools import lru_cache
import cv2
import numpy as np


INK = (246, 248, 250)
MUTED = (191, 200, 210)
CYAN = (236, 203, 119)
LAVENDER = (231, 197, 192)
_BOARD_BLUR = False
_BOARD_LITE = False
_BOARD_GLASS = False
_BOARD_GLASS_FAST = False
_NATIVE_TINT = None
_NATIVE_CONSTANT = None


def configure_board_blur(enabled=True, flat=False, *, quality=None):
    """Use glass/glass_fast/flat quality; omitted quality keeps legacy flags.

    Glass softens windows covering at least 6% of the frame and 20% of its
    height. Short cards stay flat; both qualities keep the camera untinted.
    Glass_fast limits softening to the top 64 scaled pixels of each window.
    """
    global _BOARD_BLUR, _BOARD_LITE, _BOARD_GLASS, _BOARD_GLASS_FAST
    global _NATIVE_TINT, _NATIVE_CONSTANT
    if quality not in (None, "flat", "glass", "glass_fast"):
        raise ValueError("Board quality must be 'flat', 'glass' or 'glass_fast'")
    _BOARD_BLUR = bool(enabled)
    _BOARD_LITE = bool(enabled and (quality is not None or flat))
    _BOARD_GLASS = bool(enabled and quality in ("glass", "glass_fast"))
    _BOARD_GLASS_FAST = bool(enabled and quality == "glass_fast")
    _NATIVE_TINT = _NATIVE_CONSTANT = None
    _board_masks.cache_clear()
    _board_glow_layer.cache_clear()
    if enabled:
        try:
            from frame_color import native_blend_used, tint_mix_masked, blend_constant_masked
            if native_blend_used:
                _NATIVE_TINT, _NATIVE_CONSTANT = tint_mix_masked, blend_constant_masked
        except ImportError:
            pass


def blend_background(image):
    """Keep the original 94/6 tint; board mode can fuse the pixel loop."""
    if _BOARD_LITE:
        return
    if _NATIVE_CONSTANT is not None:
        if _NATIVE_CONSTANT(image, None, (32, 37, 42), 0.06):
            return
    cv2.addWeighted(image, 0.94, np.full_like(image, (32, 37, 42)), 0.06, 0, image)


def _gaussian_blur(image, sigma):
    if not _BOARD_BLUR:
        return cv2.GaussianBlur(image, (0, 0), sigma)
    height, width = image.shape[:2]
    scale = min(0.25, 64.0 / max(width, height))
    small = cv2.resize(image, (max(1, round(width * scale)),
                              max(1, round(height * scale))),
                       interpolation=cv2.INTER_LINEAR)
    small = cv2.GaussianBlur(small, (0, 0), max(0.1, sigma * scale))
    return cv2.resize(small, (width, height), interpolation=cv2.INTER_LINEAR)


def unit(width: int, height: int) -> float:
    return max(0.4, min(width / 1280.0, height / 720.0))


def rect_fraction(width: int, height: int, x1: float, y1: float, x2: float, y2: float) -> tuple[int, int, int, int]:
    return (round(width * x1), round(height * y1), round(width * x2), round(height * y2))


def _rounded_mask(width: int, height: int, radius: int) -> np.ndarray:
    radius = max(1, min(radius, width // 2, height // 2))
    mask = np.zeros((height, width), dtype=np.uint8)
    cv2.rectangle(mask, (radius, 0), (width - radius - 1, height - 1), 255, -1)
    cv2.rectangle(mask, (0, radius), (width - 1, height - radius - 1), 255, -1)
    for cx, cy in ((radius, radius), (width - radius - 1, radius),
                   (radius, height - radius - 1), (width - radius - 1, height - radius - 1)):
        cv2.circle(mask, (cx, cy), radius, 255, -1, cv2.LINE_AA)
    return mask


@lru_cache(maxsize=16)
def _board_masks(width, height, radius):
    """Cache geometry only; camera pixels never enter the cache."""
    mask = _rounded_mask(width, height, radius)
    edge = cv2.Canny(mask, 50, 100)
    mask.flags.writeable = False
    edge.flags.writeable = False
    return mask, edge


def glass(image: np.ndarray, rect: tuple[int, int, int, int], *, strength: float = 0.52,
          tint: tuple[int, int, int] = (41, 44, 47), radius: int = 22,
          border: tuple[int, int, int] = (183, 198, 211)) -> None:
    """Blur only under a rounded card, then tint it without covering the camera."""
    height, width = image.shape[:2]
    x1, y1, x2, y2 = rect
    x1, x2 = max(0, x1), min(width, x2)
    y1, y2 = max(0, y1), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = image[y1:y2, x1:x2]
    if _BOARD_BLUR:
        mask, edge = _board_masks(x2 - x1, y2 - y1, radius)
    else:
        mask = _rounded_mask(x2 - x1, y2 - y1, radius)
        edge = cv2.Canny(mask, 50, 100)
    flat_panel = _BOARD_LITE and not (
        _BOARD_GLASS and (x2 - x1) * (y2 - y1) >= width * height * 0.06
        and y2 - y1 >= height * 0.20)
    parts = [(roi, mask, flat_panel)]
    if _BOARD_GLASS_FAST and not flat_panel:
        header = min(roi.shape[0], max(1, round(64 * unit(width, height))))
        parts = [(roi[:header], mask[:header], False)]
        if header < roi.shape[0]:
            parts.append((roi[header:], mask[header:], True))
    for part, part_mask, flat_part in parts:
        # Fusing the flat two-stage blend changes at most one byte rounding level.
        flat_blended = (flat_part and _NATIVE_CONSTANT is not None
                        and _NATIVE_CONSTANT(part, part_mask, tint, strength / 2))
        if not flat_blended:
            soft = part if flat_part else _gaussian_blur(part, 12)
            if flat_part or _NATIVE_TINT is None or not _NATIVE_TINT(part, soft, part_mask, strength, tint):
                tinted = cv2.addWeighted(soft, 0.50, np.full_like(soft, tint), 0.50, 0)
                new = cv2.addWeighted(part, 1 - strength, tinted, strength, 0)
                np.copyto(part, new, where=part_mask[:, :, None] > 0)
    if _NATIVE_CONSTANT is None or not _NATIVE_CONSTANT(roi, edge, border, 0.42):
        edge_color = np.full_like(roi, border)
        roi[edge > 0] = cv2.addWeighted(roi[edge > 0], 0.58, edge_color[edge > 0], 0.42, 0)


def fill_round(image: np.ndarray, rect: tuple[int, int, int, int], color: tuple[int, int, int],
               opacity: float, radius: int = 18) -> None:
    x1, y1, x2, y2 = rect
    height, width = image.shape[:2]
    x1, x2 = max(0, x1), min(width, x2)
    y1, y2 = max(0, y1), min(height, y2)
    if x2 <= x1 or y2 <= y1:
        return
    roi = image[y1:y2, x1:x2]
    mask = (_board_masks(x2 - x1, y2 - y1, radius)[0] if _BOARD_BLUR
            else _rounded_mask(x2 - x1, y2 - y1, radius))
    if _NATIVE_CONSTANT is not None and _NATIVE_CONSTANT(roi, mask, color, opacity):
        return
    tinted = cv2.addWeighted(roi, 1 - opacity, np.full_like(roi, color), opacity, 0)
    np.copyto(roi, tinted, where=mask[:, :, None] > 0)


def label(image: np.ndarray, value: str, x: int, y: int, scale: float = 0.74,
          color: tuple[int, int, int] = INK, thickness: int = 1) -> None:
    cv2.putText(image, value, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color,
                thickness, cv2.LINE_AA)


def centered_label(image: np.ndarray, value: str, rect: tuple[int, int, int, int],
                   scale: float, color: tuple[int, int, int] = INK, thickness: int = 1) -> None:
    size, baseline = cv2.getTextSize(value, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness)
    x1, y1, x2, y2 = rect
    label(image, value, round((x1 + x2 - size[0]) / 2),
          round((y1 + y2 + size[1] - baseline) / 2), scale, color, thickness)


@lru_cache(maxsize=16)
def _board_glow_layer(width, height, center_x, center_y, radius, color):
    """Cache only additive artwork, never camera pixels or blended frames."""
    layer = np.zeros((height, width, 3), dtype=np.uint8)
    cv2.circle(layer, (center_x, center_y), radius * 2, color, -1, cv2.LINE_AA)
    layer = _gaussian_blur(layer, max(3, radius * 0.8))
    layer = cv2.convertScaleAbs(layer, alpha=0.22)
    layer.flags.writeable = False
    return layer


def glow(image: np.ndarray, center: tuple[int, int], radius: int, color: tuple[int, int, int]) -> None:
    if radius < 2:
        return
    height, width = image.shape[:2]
    extent = radius * 4
    x1, y1 = max(0, center[0] - extent), max(0, center[1] - extent)
    x2, y2 = min(width, center[0] + extent), min(height, center[1] + extent)
    if x2 <= x1 or y2 <= y1:
        return
    roi = image[y1:y2, x1:x2]
    if _BOARD_BLUR:
        layer = _board_glow_layer(x2 - x1, y2 - y1, center[0] - x1,
                                  center[1] - y1, radius, tuple(color))
        # Pre-rounding the additive light differs from addWeighted by at most 1.
        cv2.add(layer, roi, dst=roi)
        return
    layer = np.zeros_like(roi)
    cv2.circle(layer, (center[0] - x1, center[1] - y1), radius * 2, color, -1, cv2.LINE_AA)
    layer = _gaussian_blur(layer, max(3, radius * 0.8))
    cv2.addWeighted(layer, 0.22, roi, 1.0, 0, dst=roi)
