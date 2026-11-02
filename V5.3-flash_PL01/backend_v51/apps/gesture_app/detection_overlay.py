"""In-place model overlays in the mirrored, unzoomed display coordinate system.

The caller supplies only fresh detections. This module keeps no detection state
and applies display zoom once; mirroring already happened upstream.
"""
from pathlib import Path
import math
import time

import cv2
import numpy as np


CLASS_COLORS_BGR = {
    'open_palm': (80, 220, 80),
    'side_palm': (255, 190, 70),
    'pinch_point': (220, 90, 255),
    'thumbs_up': (180, 180, 180),
}
CLASS_LABELS_ZH = {
    'open_palm': '张手',
    'side_palm': '侧掌',
    'pinch_point': '捏取',
    'thumbs_up': '拇指',
}
_MASKS = {}
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def _mask(label):
    if label not in CLASS_LABELS_ZH:
        return None
    if label not in _MASKS:
        path = Path(__file__).with_name('assets') / ('label_' + label + '.png')
        mask = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if mask is None:
            raise RuntimeError('Missing detection label mask: ' + str(path))
        _MASKS[label] = mask
    return _MASKS[label]


def _label(image, text, mask, left, top, color):
    """Blend only the small label ROI; rectangles returned here are half-open."""
    height, width = image.shape[:2]
    (text_width, text_height), baseline = cv2.getTextSize(text, _FONT, .48, 1)
    mask_height, mask_width = mask.shape if mask is not None else (0, 0)
    text_left = 6 + (mask_width + 8 if mask_width else 0)
    label_width = min(width, text_left + text_width + 6)
    label_height = min(height, max(24, mask_height + 8, text_height + baseline + 8))
    left = max(0, min(left, width - label_width))
    top = max(0, min(top, height - label_height))
    right, bottom = left + label_width, top + label_height
    roi = image[top:bottom, left:right]
    roi[:] = ((roi.astype(np.uint16) * 64 + 18 * 192) // 256).astype(np.uint8)
    roi[:, :3] = color
    glyph_left = min(right, left + 6)
    glyph_top = min(bottom, top + max(0, (label_height - mask_height) // 2))
    glyph_right = min(right, glyph_left + mask_width)
    glyph_bottom = min(bottom, glyph_top + mask_height)
    if mask is not None and glyph_right > glyph_left and glyph_bottom > glyph_top:
        glyph_roi = image[glyph_top:glyph_bottom, glyph_left:glyph_right]
        alpha = mask[:glyph_bottom-glyph_top, :glyph_right-glyph_left, None].astype(np.uint16)
        glyph_roi[:] = ((glyph_roi.astype(np.uint16) * (255-alpha) + 255*alpha + 127) // 255).astype(np.uint8)
    cv2.putText(roi, text, (text_left, (label_height + text_height) // 2),
                _FONT, .48, (255, 255, 255), 1, cv2.LINE_AA)
    return (left, top, right, bottom), (glyph_left, glyph_top, glyph_right, glyph_bottom)


def draw_detections(image, detections, zoom=1.0, *, hud=None, details=False):
    """Return ``(image, drawing_metadata)`` after drawing into the same BGR array.

    Detection.box coordinates refer to this image's unzoomed dimensions. Boxes
    use inclusive clipped pixel endpoints. Metadata label/draw rectangles are
    half-open and bound all changed pixels. Confidence is retained unchanged in
    metadata and rounded to two decimals for the visible ASCII label.

    ``hud`` is an optional short ASCII status string. It never creates boxes.
    Only small label ROIs allocate temporary arrays; the frame is never copied.
    """
    height, width = image.shape[:2]
    rows = []
    if not width or not height:
        return image, rows
    center_x, center_y = width / 2, height / 2
    for detection in detections:
        x1, y1, x2, y2 = detection.box
        x1 = round((x1 - center_x) * zoom + center_x)
        x2 = round((x2 - center_x) * zoom + center_x)
        y1 = round((y1 - center_y) * zoom + center_y)
        y2 = round((y2 - center_y) * zoom + center_y)
        if x2 < x1 or y2 < y1 or x2 < 0 or y2 < 0 or x1 >= width or y1 >= height:
            continue
        x1, x2 = max(0, x1), min(width-1, x2)
        y1, y2 = max(0, y1), min(height-1, y2)
        color = CLASS_COLORS_BGR.get(detection.label, (255, 255, 255))
        cv2.rectangle(image, (x1, y1), (x2, y2), color, 2)
        mask = _mask(detection.label)
        label_height = max(24, mask.shape[0] + 8 if mask is not None else 0)
        label_top = y1-label_height-3 if y1 >= label_height+3 else y1+3
        text = f'{detection.label} {detection.confidence:.2f}'
        objectness = detection.objectness
        class_probability = detection.class_probability
        if details:
            if objectness is not None:
                text += f' obj={objectness:.3f}'
            if class_probability is not None:
                text += f' cls={class_probability:.3f}'
        label_rect, glyph_rect = _label(image, text, mask, x1, label_top, color)
        draw_rect = (min(max(0, x1-1), label_rect[0]),
                     min(max(0, y1-1), label_rect[1]),
                     max(min(width, x2+2), label_rect[2]),
                     max(min(height, y2+2), label_rect[3]))
        rows.append(dict(label=detection.label, label_zh=CLASS_LABELS_ZH.get(detection.label, ''),
                         confidence=float(detection.confidence), box=(x1, y1, x2, y2), color_bgr=color,
                         objectness=None if objectness is None else float(objectness),
                         class_probability=None if class_probability is None else float(class_probability),
                         text=text, label_rect=label_rect, label_zh_rect=glyph_rect,
                         draw_rect=draw_rect))
    if hud:
        _label(image, str(hud), None, 8, 6, (180, 180, 180))
    return image, rows


def draw_application_detections(image, app, *, hud=False, details=False, now=None):
    """Overlay the application's last consumed result, rechecking age at draw time.

    Call on the display thread immediately after ``app.render_frame``. Pending
    worker results are deliberately ignored until the application consumes them.
    Returned status contains only JSON-compatible scalars, lists and dictionaries.
    """
    now = time.monotonic() if now is None else float(now)
    source = app.source_monotonic
    source = float(source) if source is not None else None
    if source is not None and (not math.isfinite(source) or source < 0):
        source = None
    age = now-source if source is not None and math.isfinite(now) else None
    fresh = (age is not None and 0 <= age <= app.stale_s
             and not getattr(app, '_expired', False))
    detections = app.detections if fresh else []
    _, boxes = draw_detections(image, detections, app.display_zoom, details=details)
    counts = {}
    for box in boxes:
        counts[box['label']] = counts.get(box['label'], 0) + 1
    mode = str(app.hub.mode)
    age_ms = age*1000 if age is not None else None
    detected_counts = {}
    for detection in detections:
        detected_counts[detection.label] = detected_counts.get(detection.label, 0) + 1
    palm_count = detected_counts.get('open_palm', 0)
    status = ('WAITING' if app.last_result_id < 0 else 'EXPIRED' if not fresh
              else 'FRESH' if detections else 'NO_DETECTION')
    age_text = '-' if age_ms is None else f'{age_ms:.0f}ms'
    hud_text = (f'mode={mode} palms={palm_count} boxes={len(boxes)} '
                f'zoom={app.display_zoom:.2f}x age={age_text} '
                f'{app.state.zoom_state} {status}')
    metadata = dict(result_id=int(app.last_result_id), source_monotonic=source,
                    age_ms=age_ms, count=len(boxes), mode=mode,
                    class_counts=counts, boxes=boxes, detected_count=len(detections),
                    detected_class_counts=detected_counts, palm_count=palm_count,
                    zoom=float(app.display_zoom), zoom_target=float(app.state.zoom),
                    zoom_state=app.state.zoom_state, detection_status=status,
                    hud_text=hud_text)
    if hud and image.shape[0] and image.shape[1]:
        _label(image, hud_text, None, 8, 6, (180, 180, 180))
    return image, metadata
