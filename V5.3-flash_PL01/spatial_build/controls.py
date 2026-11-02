"""Time based intent locking for fresh gesture observations.

Only ``observe`` consumes model results. Rendering calls ``tick`` for timeout
handling; it cannot confirm a press or manufacture a new gesture observation.
Coordinates remain the application's mirrored, unscaled video coordinates.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable


class GestureController:
    """One pinch, single-palm rotation or two-palm zoom owns the interaction.

    ``hit_test(point)`` returns ``(intent, target)`` where intent is ``button``,
    ``select``, ``empty``, ``place``, ``drag`` or ``rotate``. Button, select and
    empty emit one click after confirmation. Place requires two consecutive
    fresh pinches on the same cell, then emits place_release on the first fresh
    no-pinch result. Moving to another legal cell restarts that two-result run.
    Drag and rotation emit start/move/end events.
    Zoom's ``ratio`` is incremental; ``total_ratio`` is cumulative from start.
    A recovered move has zero delta / ratio 1, so missing detections never make
    the structure jump. The application retains selection independently.
    """

    CLICK_INTENTS = frozenset(("button", "select", "empty"))
    MOVE_INTENTS = frozenset(("drag", "rotate"))
    VALID_INTENTS = CLICK_INTENTS | MOVE_INTENTS | {"place"}

    def __init__(self, confirm_seconds: float = 0.16,
                 dropout_grace_seconds: float = 0.45,
                 watchdog_seconds: float = 1.0,
                 maximum_jump_fraction: float = 0.40):
        values = (confirm_seconds, dropout_grace_seconds, watchdog_seconds,
                  maximum_jump_fraction)
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in values):
            raise ValueError("gesture timing and jump limits must be positive")
        self.confirm_seconds = float(confirm_seconds)
        self.dropout_grace_seconds = float(dropout_grace_seconds)
        self.watchdog_seconds = float(watchdog_seconds)
        self.maximum_jump_fraction = float(maximum_jump_fraction)
        self.reset(require_release=False)

    def reset(self, require_release: bool = True) -> None:
        """Clear stage-local interaction; optionally require an actual release.

        Use the default on a stage change to prevent the pinch that selected a
        level or Check from also selecting a block on the newly displayed view.
        """
        self.phase = "blocked" if require_release else "idle"
        self.pointer = None
        self._intent = None
        self._target = None
        self._source = None
        self._pending_at = None
        self._last_point = None
        self._start_point = None
        self._last_distance = None
        self._total_ratio = 1.0
        self._last_observation = None
        self._missing_since = None
        self._release_since = None
        self._diagonal = 1.0

    @property
    def active_intent(self):
        return self._intent if self.phase in ("active", "latched") else None

    @property
    def target(self):
        return self._target if self.phase in ("pending", "active", "latched") else None

    @staticmethod
    def _detection_value(detection, key, default=None):
        return detection.get(key, default) if isinstance(detection, dict) else getattr(detection, key, default)

    @classmethod
    def _center(cls, detection):
        center = cls._detection_value(detection, "center")
        if center is None:
            box = cls._detection_value(detection, "box")
            if box is None or len(box) != 4:
                return None
            center = ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)
        try:
            point = (float(center[0]), float(center[1]))
        except (TypeError, ValueError, IndexError):
            return None
        return point if all(math.isfinite(v) for v in point) else None

    def _points(self, detections: Iterable, label: str):
        candidates = []
        for d in detections:
            if self._detection_value(d, "label") != label:
                continue
            point = self._center(d)
            if point is None:
                continue
            try:
                confidence = float(self._detection_value(d, "confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if math.isfinite(confidence):
                candidates.append((confidence, point))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates

    def _event(self, kind, now, **extra):
        event = {"kind": kind, "intent": self._intent, "target": self._target,
                 "point": self.pointer, "time": float(now)}
        if self._source is not None:
            event["source"] = self._source
        event.update(extra)
        return event

    def _clear(self, blocked=False):
        self.phase = "blocked" if blocked else "idle"
        self.pointer = None
        self._intent = self._target = None
        self._source = None
        self._pending_at = self._last_point = self._start_point = None
        self._last_distance = None
        self._total_ratio = 1.0
        self._missing_since = self._release_since = None

    def _finish(self, now, reason="release", cancelled=False):
        events = []
        if self.phase in ("active", "latched"):
            if cancelled:
                events.append(self._event("cancel", now, reason=reason))
            elif self._intent == "place":
                events.append(self._event("place_release", now))
            elif self._intent in self.MOVE_INTENTS or self._intent == "zoom":
                events.append(self._event(self._intent + "_end", now, reason=reason))
        self._clear(blocked=cancelled)
        return events

    def tick(self, now: float):
        """Timeout active actions without consuming cached model results."""
        now = float(now)
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        if self._last_observation is None:
            return []
        if self.phase in ("pending", "active", "latched"):
            # Placement keeps its explicit candidate through UI preparation;
            # only a fresh observation or an application reset may end it.
            if self._intent != "place" and now - self._last_observation >= self.watchdog_seconds:
                return self._finish(now, "watchdog", cancelled=True)
            if (self._intent != "place" and self._missing_since is not None
                    and now - self._missing_since >= self.dropout_grace_seconds):
                return self._finish(now)
        return []

    def _missing(self, now):
        # A not-yet-confirmed action has no side effects and must reconfirm.
        if self.phase == "pending":
            self._clear()
        elif self.phase in ("active", "latched") and self._missing_since is None:
            self._missing_since = now

    def _begin_pending(self, intent, target, point, now, distance=None,
                       source="pinch"):
        self.phase = "pending"
        self._intent, self._target = intent, target
        self._source = source
        self.pointer = self._last_point = self._start_point = point
        self._pending_at = now
        self._missing_since = None
        self._last_distance = distance

    def _confirm(self, now, point, distance=None):
        self.pointer = self._last_point = self._start_point = point
        self._last_distance = distance
        if self._intent == "place":
            self.phase = "latched"
            return []
        if self._intent in self.CLICK_INTENTS:
            self.phase = "latched"
            return [self._event("click", now)]
        self.phase = "active"
        extra = {"start_point": point, "delta": (0.0, 0.0), "recovered": False}
        if self._intent == "zoom":
            extra.update(distance=distance, ratio=1.0, total_ratio=1.0)
        return [self._event(self._intent + "_start", now, **extra)]

    def observe(self, detections, w: int, h: int, now: float,
                hit_test: Callable, allow_zoom: bool = False,
                allow_palm_rotate: bool = False):
        """Consume ONE fresh inference result and return zero or more events.

        Repeated/non-increasing observation timestamps are ignored. Pass model
        results here only when fresh; call ``tick`` on display frames instead.
        Model thresholds and gesture labels are not changed by this controller.
        In observation mode, enable ``allow_palm_rotate``: exactly one open
        palm controls rotation, and a second palm explicitly upgrades that
        intent to zoom. A temporarily missing second palm never immediately
        changes zoom into rotation. Pinches remain separately intent-locked.
        """
        now = float(now)
        if not math.isfinite(now) or w <= 0 or h <= 0:
            raise ValueError("positive image dimensions and finite time required")
        if self._last_observation is not None and now <= self._last_observation:
            return []
        events = self.tick(now)
        self._last_observation = now
        self._diagonal = math.hypot(w, h)
        detections = list(detections)
        pinches = self._points(detections, "pinch_point")
        palms = self._points(detections, "open_palm")[:2]
        zoom = None
        if allow_zoom and len(palms) == 2:
            distance = math.dist(palms[0][1], palms[1][1])
            if distance >= max(10.0, self._diagonal * 0.025):
                midpoint = tuple((a + b) / 2.0 for a, b in zip(palms[0][1], palms[1][1]))
                zoom = (midpoint, distance)

        if self.phase == "blocked":
            # Safety/stage latch: continuing to hold the same gesture cannot
            # become a new action. Only fresh empty results clear this latch.
            if pinches or zoom is not None or (allow_palm_rotate and palms):
                self._release_since = None
            elif self._release_since is None:
                self._release_since = now
            elif now - self._release_since >= self.dropout_grace_seconds:
                self._clear()
            return events

        if self._source == "palm" and self.phase in ("pending", "active"):
            # A second palm is an explicit change of operation. Stop rotation
            # now, then confirm zoom from fresh observations before scaling.
            # A simultaneous pinch must not steal a held palm's intent.
            if zoom is not None:
                events.extend(self._finish(now, "two_palms"))
                self._begin_pending("zoom", None, zoom[0], now, zoom[1],
                                    source="palms")
                return events
            if not allow_palm_rotate or len(palms) != 1:
                self._missing(now)
                return events
            point = palms[0][1]
            recovered = self._missing_since is not None
            if math.dist(point, self._last_point) > self.maximum_jump_fraction * self._diagonal:
                events.extend(self._finish(now, "pointer_jump", cancelled=True))
                return events
            if self.phase == "pending":
                self.pointer = self._last_point = point
                if now - self._pending_at >= self.confirm_seconds:
                    events.extend(self._confirm(now, point))
                return events
            previous = self._last_point
            self._missing_since = None
            self.pointer = self._last_point = point
            if recovered:
                self._start_point = point
            delta = (0.0, 0.0) if recovered else (point[0] - previous[0], point[1] - previous[1])
            events.append(self._event("rotate_move", now, delta=delta,
                                      start_point=self._start_point,
                                      recovered=recovered))
            return events

        if self._intent == "zoom" and self.phase in ("pending", "active"):
            if zoom is None:
                # Even an unconfirmed two-palm candidate reserves the mode
                # during a brief second-hand miss; otherwise a single result
                # could be immediately reinterpreted as rotation.
                if self.phase == "pending":
                    if self._missing_since is None:
                        self._missing_since = now
                    self._pending_at = now
                else:
                    self._missing(now)
                return events
            point, distance = zoom
            if self.phase == "pending":
                if self._missing_since is not None:
                    self._missing_since = None
                    self._pending_at = now
                self.pointer = point
                if now - self._pending_at >= self.confirm_seconds:
                    events.extend(self._confirm(now, point, distance))
                return events
            recovered = self._missing_since is not None
            ratio = 1.0 if recovered else distance / max(self._last_distance, 1.0)
            # A single corrupt observation must not produce an extreme jump.
            if not recovered and not 0.25 <= ratio <= 4.0:
                events.extend(self._finish(now, "zoom_jump", cancelled=True))
                return events
            self._missing_since = None
            self.pointer = point
            self._last_distance = distance
            self._total_ratio *= ratio
            events.append(self._event("zoom_move", now, distance=distance,
                                      ratio=ratio, total_ratio=self._total_ratio,
                                      recovered=recovered))
            return events

        if self.phase in ("pending", "active", "latched"):
            if not pinches:
                if self.phase == "latched" and self._intent == "place":
                    events.extend(self._finish(now))
                else:
                    self._missing(now)
                return events
            point = min(pinches, key=lambda item: math.dist(item[1], self._last_point))[1]
            recovered = self._missing_since is not None
            if (self._intent != "place"
                    and math.dist(point, self._last_point) > self.maximum_jump_fraction * self._diagonal):
                events.extend(self._finish(now, "pointer_jump", cancelled=True))
                return events
            if self.phase == "pending":
                # Crossing into a different target before confirmation starts
                # a new confirmation period; afterwards the intent is locked.
                hit = hit_test(point)
                if hit is None or len(hit) != 2 or hit[0] not in self.VALID_INTENTS:
                    self._clear()
                    return events
                if hit[0] != self._intent or hit[1] != self._target:
                    self._begin_pending(hit[0], hit[1], point, now)
                    return events
                self.pointer = self._last_point = point
                if self._intent == "place" or now - self._pending_at >= self.confirm_seconds:
                    events.extend(self._confirm(now, point))
                return events
            if self._intent == "place":
                hit = hit_test(point)
                if hit != ("place", self._target):
                    if hit is not None and len(hit) == 2 and hit[0] == "place":
                        self._begin_pending(hit[0], hit[1], point, now)
                    else:
                        events.extend(self._finish(now, "target_changed", cancelled=True))
                        # Leaving the placement area invalidates the run, but
                        # a held pinch may enter a legal cell and start anew.
                        self._clear()
                    return events
            self._missing_since = None
            previous = self._last_point
            self.pointer = self._last_point = point
            if self.phase == "active":
                delta = (0.0, 0.0) if recovered else (point[0] - previous[0], point[1] - previous[1])
                if recovered:
                    self._start_point = point
                events.append(self._event(self._intent + "_move", now,
                                          delta=delta, start_point=self._start_point,
                                          recovered=recovered))
            return events

        # Pinch has priority when idle. Once zoom starts, both kinds stay locked
        # to their original operation until release or an explicit cancellation.
        if pinches:
            point = pinches[0][1]
            hit = hit_test(point)
            self.pointer = point
            if hit is not None and len(hit) == 2 and hit[0] in self.VALID_INTENTS:
                self._begin_pending(hit[0], hit[1], point, now)
        elif zoom is not None:
            self._begin_pending("zoom", None, zoom[0], now, zoom[1], source="palms")
        elif allow_palm_rotate and len(palms) == 1:
            self._begin_pending("rotate", "model", palms[0][1], now,
                                source="palm")
        else:
            self.pointer = None
        return events
