"""A side-palm plane can strike an orb from either side in camera space.

Geometry and physics use one screen height as their common distance unit, so
an angled plane has the same normal on wide and portrait camera frames.
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import math
import time
import cv2
import numpy as np
from .core import Detection
from .palm_plane import estimate_side_plane
from .spatial_ui import CYAN, INK, LAVENDER, MUTED, centered_label, glass, glow, label, rect_fraction, unit


@dataclass
class _PalmPlane:
    x: float
    y: float
    tx: float
    ty: float
    half_length: float
    vx: float = 0.0
    vy: float = 0.0

    @property
    def normal(self) -> tuple[float, float]:
        return -self.ty, self.tx


class PalmBounce:
    """A 30-second round: swing a side palm toward the orb to score hits."""

    name = "Side Palm Ball"
    AREA = (0.08, 0.24, 0.92, 0.84)
    GRAVITY = 0.16
    TRACK_TIMEOUT = 0.75
    RECOVERY_SECONDS = 0.75
    ROUND_SECONDS = 30.0
    HIT_SPEED = 0.10

    def __init__(self) -> None:
        self._trace = None
        self._trace_context = None
        self.phase = "ready"
        self.score = 0
        self.best = 0
        # Position stays normalised per frame dimension for the drawing API.
        # Velocities and plane coordinates instead use screen heights/second.
        self.ball_x, self.ball_y = 0.5, 0.45
        self.ball_vx = self.ball_vy = 0.0
        self._plane: _PalmPlane | None = None
        self._motion_start: _PalmPlane | None = None
        self._motion_pending = False
        self._last_observed: float | None = None
        self._last_source: float | None = None
        self._expired_plane: _PalmPlane | None = None
        self._expired_source: float | None = None
        self._expired_stable = 0
        self._resume_until = 0.0
        self._resume_remaining: float | None = None
        self._last_render: float | None = None
        self._stable_palms = 0
        self._launch_at = 0.0
        self._round_end = 0.0
        self._last_hit = -math.inf
        self._contact_latched = False
        self._hit_flash = 0.0
        self._trail: deque[tuple[float, float]] = deque(maxlen=14)

    def set_trace(self, callback) -> None:
        self._trace = callback
        self._trace_context = None

    def _emit(self, event) -> None:
        payload = dict(self._trace_context or ())
        if event.get('monotonic') is None:
            event.pop('monotonic', None)
        payload.update(event)
        payload['application'] = 'palm_bounce'
        self._trace(payload)

    @staticmethod
    def window_rect(width: int, height: int) -> tuple[int, int, int, int]:
        return rect_fraction(width, height, 0.055, 0.16, 0.945, 0.86)

    @staticmethod
    def replay_rect(width: int, height: int) -> tuple[int, int, int, int]:
        return rect_fraction(width, height, 0.40, 0.61, 0.60, 0.69)

    def hit_target(self, point: tuple[float, float], width: int, height: int):
        x1, y1, x2, y2 = self.replay_rect(width, height)
        if self.phase != "finished" or not (x1 <= point[0] <= x2 and y1 <= point[1] <= y2):
            return None
        return "replay"

    def click(self, point: tuple[float, float], width: int, height: int) -> bool:
        if self.hit_target(point, width, height) is None:
            return False
        best = self.best
        trace = self._trace
        self.__init__()
        self._trace = trace
        self.best = best
        return True

    def _lose_plane(self, now: float | None = None, recover: bool = False) -> None:
        if self._trace is not None and (self._plane is not None or self.phase == 'countdown'):
            self._emit(dict(kind='ball_observe', action='plane_lost',
                            reason='tracking_expired' if recover else 'no_side_palm',
                            monotonic=now, phase=self.phase))
        expiry = (self._last_observed + self.TRACK_TIMEOUT
                  if self._last_observed is not None else now)
        if recover:
            # Historical measurements may estimate the next fresh sample's
            # velocity; they are never a collider while tracking is expired.
            self._expired_plane, self._expired_source = self._plane, self._last_observed
            self._expired_stable = self._stable_palms
        else:
            self._expired_plane = self._expired_source = None
            self._expired_stable = 0
        if recover and self.phase == "countdown":
            self._resume_remaining = max(0.0, self._launch_at - expiry)
            self._resume_until = expiry + self.RECOVERY_SECONDS
        elif not recover:
            self._resume_remaining = None
            self._resume_until = 0.0
        self._plane = self._motion_start = None
        self._motion_pending = False
        self._last_observed = None
        self._stable_palms = 0
        self._contact_latched = False
        if self.phase == "countdown":
            self.phase = "ready"
            if self._trace is not None:
                self._emit(dict(kind='ball_action', action='countdown_cancel',
                                trigger='plane_loss', monotonic=now,
                                reason='tracking_expired' if recover else 'no_side_palm'))

    def observe(self, detections: list[Detection], width: int, height: int,
                now: float | None = None, image: np.ndarray | None = None,
                source_now: float | None = None, trace_context=None) -> None:
        """Fresh inference: missing side palms immediately remove the collider.

        Reacquisition and implausible jumps reset motion instead of sweeping a
        collider across the scene. Continuous 0.4/0.5-second observations retain
        both the countdown and a measured palm velocity.
        """
        now = time.perf_counter() if now is None else now
        source_now = now if source_now is None else source_now
        if (not math.isfinite(now) or not math.isfinite(source_now)
                or source_now > now
                or (self._last_source is not None and source_now <= self._last_source)):
            if self._trace is not None:
                self._emit(dict(kind='ball_observe', action='not_applied',
                                reason='invalid_or_repeated_source', monotonic=now))
            return
        self._last_source = source_now
        if now - source_now > self.TRACK_TIMEOUT:
            if self._trace is not None:
                self._emit(dict(trace_context or (), kind='ball_observe', action='not_applied',
                                reason='ball_source_stale', monotonic=now))
            if self._plane is not None and not self._tracked_plane(now):
                self._lose_plane(now, recover=True)
            return
        if self._trace is not None:
            self._trace_context = trace_context
        palms = [d for d in detections if d.label == "side_palm"
                 and d.box[2] > d.box[0] and d.box[3] > d.box[1]]
        if not palms or width <= 0 or height <= 0:
            self._lose_plane()
            return
        if self._plane is not None and not self._tracked_plane(now):
            self._lose_plane(now, recover=True)
        old = self._plane if self._plane is not None else self._expired_plane
        old_source = self._last_observed if self._last_observed is not None else self._expired_source
        elapsed = source_now - old_source if old_source is not None else math.inf
        continuous = old is not None and 0 < elapsed <= self.TRACK_TIMEOUT
        if continuous:
            palm = min(palms, key=lambda d: math.hypot(d.center[0] / height - old.x,
                                                      d.center[1] / height - old.y)
                       - 0.035 * d.confidence)
        else:
            palm = max(palms, key=lambda d: d.confidence)
        geometry = estimate_side_plane(palm, width, height, image=image)
        tx, ty = geometry.tangent
        tangent_norm = max(1e-9, math.hypot(tx, ty))
        tx, ty = tx / tangent_norm, ty / tangent_norm
        x, y = geometry.center[0] / height, geometry.center[1] / height
        half = max(0.035, min(0.25, geometry.half_length / height))
        if continuous and math.hypot(x - old.x, y - old.y) <= 0.34:
            if tx * old.tx + ty * old.ty < 0:
                tx, ty = -tx, -ty
            # A sudden axis flip is a segmentation change, not a swing.
            continuous = tx * old.tx + ty * old.ty >= 0.60
        else:
            continuous = False
        if continuous:
            self._stable_palms = max(self._stable_palms, self._expired_stable)
            x, y = old.x * 0.15 + x * 0.85, old.y * 0.15 + y * 0.85
            tx, ty = old.tx * 0.25 + tx * 0.75, old.ty * 0.25 + ty * 0.75
            length = max(1e-9, math.hypot(tx, ty))
            tx, ty = tx / length, ty / length
            vx, vy = (x - old.x) / max(0.015, elapsed), (y - old.y) / max(0.015, elapsed)
            if math.hypot(x - old.x, y - old.y) < 0.003:
                vx = vy = 0.0
            speed = math.hypot(vx, vy)
            if speed > 2.0:
                vx, vy = vx * 2.0 / speed, vy * 2.0 / speed
            self._plane = _PalmPlane(x, y, tx, ty, half, vx, vy)
            self._motion_start = old
            self._motion_pending = True
        else:
            self._plane = _PalmPlane(x, y, tx, ty, half)
            self._motion_start = None
            self._motion_pending = False
            self._stable_palms = 0
            self._contact_latched = False
        self._last_observed = source_now
        self._expired_plane = self._expired_source = None
        self._expired_stable = 0
        self._stable_palms += 1
        if self._trace is not None:
            self._emit(dict(kind='ball_observe', action='plane_update' if continuous else 'plane_baseline',
                            monotonic=now, source_monotonic=source_now,
                            center=[x, y], velocity=[self._plane.vx, self._plane.vy],
                            motion_pending=self._motion_pending))
        if (self.phase == "ready" and self._resume_remaining is not None
                and now <= self._resume_until and self._ready_plane(now, width, height)):
            self.phase = "countdown"
            self._launch_at = now + self._resume_remaining
            self._resume_remaining = None
            if self._trace is not None:
                self._emit(dict(kind='ball_action', action='countdown_resume',
                                monotonic=now, launch_at=self._launch_at))
        elif self.phase == "ready" and self._ready_plane(now, width, height):
            self.phase = "countdown"
            self._launch_at = now + 1.2
            self._resume_remaining = None
            if self._trace is not None:
                self._emit(dict(kind='ball_action', action='countdown_start',
                                monotonic=now, launch_at=self._launch_at))

    def _tracked_plane(self, now: float) -> bool:
        return (self._plane is not None and self._last_observed is not None
                and 0 <= now - self._last_observed <= self.TRACK_TIMEOUT)

    def _bounds(self, width: int, height: int) -> tuple[float, float, float, float]:
        left, top, right, bottom = self.AREA
        aspect = width / height
        return left * aspect, top, right * aspect, bottom

    def _valid_plane(self, now: float, width: int, height: int) -> bool:
        if not self._tracked_plane(now):
            return False
        left, top, right, bottom = self._bounds(width, height)
        plane = self._plane
        return left <= plane.x <= right and top <= plane.y <= bottom

    @staticmethod
    def _radii(width: int, height: int) -> tuple[float, float, int]:
        pixels = max(9, round(min(width, height) * 0.027))
        return pixels / width, pixels / height, pixels

    def _spawn(self, width: int, height: int) -> tuple[float, float, float, float]:
        """Choose the side of the plane with room for the orb and an approach."""
        plane = self._plane
        radius = self._radii(width, height)[1]
        left, top, right, bottom = self._bounds(width, height)
        nx, ny = plane.normal

        def room(sx: float, sy: float) -> float:
            distances = []
            if sx > 1e-6:
                distances.append((right - radius - plane.x) / sx)
            elif sx < -1e-6:
                distances.append((left + radius - plane.x) / sx)
            if sy > 1e-6:
                distances.append((bottom - radius - plane.y) / sy)
            elif sy < -1e-6:
                distances.append((top + radius - plane.y) / sy)
            return min(distances, default=0.0)

        if room(-nx, -ny) > room(nx, ny):
            nx, ny = -nx, -ny
        gap = min(0.16, max(radius + 0.025, room(nx, ny) * 0.65))
        return plane.x + nx * gap, plane.y + ny * gap, nx, ny

    def _ready_plane(self, now: float, width: int, height: int) -> bool:
        if not self._valid_plane(now, width, height):
            return False
        x, y, _, _ = self._spawn(width, height)
        left, top, right, bottom = self._bounds(width, height)
        radius = self._radii(width, height)[1]
        return left + radius <= x <= right - radius and top + radius <= y <= bottom - radius

    @staticmethod
    def _between(start: _PalmPlane, end: _PalmPlane, fraction: float) -> _PalmPlane:
        tx = start.tx + (end.tx - start.tx) * fraction
        ty = start.ty + (end.ty - start.ty) * fraction
        length = max(1e-9, math.hypot(tx, ty))
        return _PalmPlane(start.x + (end.x - start.x) * fraction,
                          start.y + (end.y - start.y) * fraction,
                          tx / length, ty / length,
                          start.half_length + (end.half_length - start.half_length) * fraction)

    @staticmethod
    def _closest(x: float, y: float, plane: _PalmPlane) -> tuple[float, float, float]:
        along = (x - plane.x) * plane.tx + (y - plane.y) * plane.ty
        along = max(-plane.half_length, min(plane.half_length, along))
        return plane.x + along * plane.tx, plane.y + along * plane.ty, along

    def _collide(self, old_x: float, old_y: float, x: float, y: float,
                 start: _PalmPlane, end: _PalmPlane, radius: float,
                 palm_velocity: tuple[float, float], now: float) -> tuple[float, float]:
        """Circle versus a finite two-sided segment, including its round ends."""
        qx, qy, along = self._closest(x, y, end)
        distance = math.hypot(x - qx, y - qy)
        if distance > radius + 0.001:
            if distance > radius + 0.024:
                self._contact_latched = False
            return x, y
        oqx, oqy, old_along = self._closest(old_x, old_y, start)
        old_distance = math.hypot(old_x - oqx, old_y - oqy)
        nx, ny = end.normal
        # The approach side controls interior contacts during a swept strike.
        # Endpoints instead use a radial contact normal.
        interior = abs(along) < end.half_length - 1e-5
        if interior:
            snx, sny = start.normal
            signed = (old_x - oqx) * snx + (old_y - oqy) * sny
            if abs(signed) < 1e-7:
                signed = (x - qx) * nx + (y - qy) * ny
            if signed < 0:
                nx, ny = -nx, -ny
        elif distance > 1e-8:
            nx, ny = (x - qx) / distance, (y - qy) / distance
        elif old_distance > 1e-8:
            nx, ny = (old_x - oqx) / old_distance, (old_y - oqy) / old_distance
        # Sliding along the palm must not pump a ball at an endpoint. Only
        # motion normal to the palm plane contributes active strike energy.
        pnx, pny = end.normal
        normal_speed = palm_velocity[0] * pnx + palm_velocity[1] * pny
        surface_vx, surface_vy = normal_speed * pnx, normal_speed * pny
        inward = (self.ball_vx - surface_vx) * nx + (self.ball_vy - surface_vy) * ny
        if inward < -0.005:
            self.ball_vx -= 1.90 * inward * nx
            self.ball_vy -= 1.90 * inward * ny
            speed = math.hypot(self.ball_vx, self.ball_vy)
            if speed > 1.65:
                self.ball_vx *= 1.65 / speed
                self.ball_vy *= 1.65 / speed
            toward_ball = surface_vx * nx + surface_vy * ny
            if (toward_ball >= self.HIT_SPEED and not self._contact_latched
                    and now - self._last_hit >= 0.22):
                self.score += 1
                self.best = max(self.best, self.score)
                self._last_hit = now
                self._hit_flash = 0.24
                if self._trace is not None:
                    self._emit(dict(kind='ball_action', action='hit', trigger='physics_collision',
                                    monotonic=now, score=self.score,
                                    motion_pending=self._motion_pending, toward_ball=toward_ball))
            self._contact_latched = True
        return qx + nx * (radius + 0.002), qy + ny * (radius + 0.002)

    def _tick(self, now: float, width: int, height: int) -> None:
        dt = 0.0 if self._last_render is None else max(0.0, min(0.25, now - self._last_render))
        self._last_render = now
        self._hit_flash = max(0.0, self._hit_flash - dt)
        if self._plane is not None and not self._tracked_plane(now):
            self._lose_plane(now, recover=True)
        active = self._valid_plane(now, width, height)
        aspect = width / height
        if self.phase in {"ready", "countdown"}:
            if self.phase == "countdown" and not self._ready_plane(now, width, height):
                self.phase = "ready"
                self._stable_palms = 0
                if self._trace is not None:
                    self._emit(dict(kind='ball_action', action='countdown_cancel',
                                    trigger='physics_tick', monotonic=now,
                                    reason='palm_not_ready'))
            if active:
                x, y, nx, ny = self._spawn(width, height)
                self.ball_x, self.ball_y = x / aspect, y
            else:
                self.ball_x, self.ball_y = 0.5, 0.45
            if self.phase == "countdown" and now >= self._launch_at:
                self.phase = "playing"
                self._round_end = now + self.ROUND_SECONDS
                self.ball_vx, self.ball_vy = -nx * 0.24, -ny * 0.24
                self._trail.clear()
                self._contact_latched = False
                if self._trace is not None:
                    self._emit(dict(kind='ball_action', action='launch',
                                    trigger='physics_tick', monotonic=now))
            self._motion_pending = False
            return
        if self.phase == "finished":
            self._motion_pending = False
            return
        if now >= self._round_end:
            self.phase = "finished"
            self._motion_pending = False
            if self._trace is not None:
                self._emit(dict(kind='ball_action', action='round_finished',
                                trigger='physics_tick', monotonic=now, score=self.score))
            return
        if dt <= 0:
            return

        end = self._plane if active else None
        moving = active and self._motion_pending and self._motion_start is not None
        start = self._motion_start if moving else end
        radius = self._radii(width, height)[1]
        palm_velocity = (end.vx, end.vy) if moving else (0.0, 0.0)
        travel = math.hypot(self.ball_vx, self.ball_vy) * dt
        if moving:
            travel += math.hypot(end.x - start.x, end.y - start.y)
            travel += max(start.half_length, end.half_length) * math.hypot(end.tx - start.tx, end.ty - start.ty)
        # Spatial and temporal steps prevent a sparsely observed moving palm
        # from tunnelling through the orb between observations.
        steps = max(1, math.ceil(dt * 180), math.ceil(travel / max(radius * 0.35, 0.003)))
        sub_dt = dt / steps
        left, top, right, bottom = self._bounds(width, height)
        x, y = self.ball_x * aspect, self.ball_y
        for index in range(steps):
            old_x, old_y = x, y
            self.ball_vy += self.GRAVITY * sub_dt
            x += self.ball_vx * sub_dt
            y += self.ball_vy * sub_dt
            if end is not None:
                plane0 = self._between(start, end, index / steps)
                plane1 = self._between(start, end, (index + 1) / steps)
                x, y = self._collide(old_x, old_y, x, y, plane0, plane1, radius, palm_velocity, now)
            if x - radius < left:
                x, self.ball_vx = left + radius, max(0.16, abs(self.ball_vx) * 0.96)
            elif x + radius > right:
                x, self.ball_vx = right - radius, -max(0.16, abs(self.ball_vx) * 0.96)
            if y - radius < top:
                y, self.ball_vy = top + radius, max(0.12, abs(self.ball_vy) * 0.93)
            elif y + radius > bottom:
                y, self.ball_vy = bottom - radius, -max(0.32, abs(self.ball_vy) * 0.93)
        self.ball_x, self.ball_y = x / aspect, y
        self._motion_pending = False
        self._trail.append((self.ball_x, self.ball_y))

    def render(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        now = time.perf_counter()
        self._tick(now, width, height)
        u = unit(width, height)
        glass(image, self.window_rect(width, height),
              strength=0.29, tint=(42, 48, 54), radius=max(10, round(28 * u)))
        label(image, "SIDE PALM BALL", round(width * 0.09), round(height * 0.208), 0.78 * u)
        remaining = max(0, math.ceil(self._round_end - now)) if self.phase == "playing" else 30
        label(image, f"HITS  {self.score:02d}   /   {remaining:02d}s",
              round(width * 0.65), round(height * 0.207), 0.65 * u, CYAN)
        left, top, right, bottom = rect_fraction(width, height, *self.AREA)
        cv2.rectangle(image, (left, top), (right, bottom), (111, 130, 144), 1, cv2.LINE_AA)

        if self._valid_plane(now, width, height) and self.phase != "finished":
            plane = self._plane
            center = (round(plane.x * height), round(plane.y * height))
            a = (round((plane.x - plane.tx * plane.half_length) * height),
                 round((plane.y - plane.ty * plane.half_length) * height))
            b = (round((plane.x + plane.tx * plane.half_length) * height),
                 round((plane.y + plane.ty * plane.half_length) * height))
            color = INK if self._hit_flash else CYAN
            glow(image, center, max(10, round(15 * u)), color)
            cv2.line(image, a, b, color, max(3, round(6 * u)), cv2.LINE_AA)
            for endpoint in (a, b):
                cv2.circle(image, endpoint, max(3, round(5 * u)), INK, -1, cv2.LINE_AA)
            cv2.circle(image, center, max(3, round(5 * u)), INK, -1, cv2.LINE_AA)
            label(image, "SIDE PALM", center[0] - round(35 * u), center[1] + round(25 * u),
                  0.38 * u, MUTED)

        if self.phase != "finished":
            _, _, radius = self._radii(width, height)
            if self.phase == "playing":
                for index, (x, y) in enumerate(self._trail):
                    trail_radius = max(2, round(radius * (index + 1) / len(self._trail) * 0.43))
                    cv2.circle(image, (round(x * width), round(y * height)), trail_radius,
                               (146, 137, 169), -1, cv2.LINE_AA)
            center = (round(self.ball_x * width), round(self.ball_y * height))
            glow(image, center, radius, LAVENDER)
            cv2.circle(image, center, radius, LAVENDER, -1, cv2.LINE_AA)
            cv2.circle(image, center, radius, INK, max(1, round(2 * u)), cv2.LINE_AA)
            cv2.circle(image, (center[0] - radius // 3, center[1] - radius // 3),
                       max(2, radius // 4), INK, -1, cv2.LINE_AA)

        if self.phase == "ready":
            hint = rect_fraction(width, height, 0.22, 0.46, 0.78, 0.60)
            glass(image, hint, strength=0.48, tint=(49, 53, 59), radius=round(18 * u))
            title = "SHOW THE SIDE OF YOUR HAND"
            detail = "Move the cyan palm plane inside the frame"
            if self._valid_plane(now, width, height):
                title, detail = "HOLD YOUR SIDE PALM STEADY", "Then swing your palm toward the orb"
            centered_label(image, title, (hint[0], hint[1], hint[2], round(height * 0.535)), 0.65 * u)
            centered_label(image, detail, (hint[0], round(height * 0.53), hint[2], hint[3]), 0.51 * u, MUTED)
        elif self.phase == "countdown":
            countdown = max(1, math.ceil((self._launch_at - now) / 0.6))
            centered_label(image, f"READY  {countdown}",
                           rect_fraction(width, height, 0.30, 0.43, 0.70, 0.53), 1.04 * u)
        elif self.phase == "finished":
            card = rect_fraction(width, height, 0.29, 0.38, 0.71, 0.73)
            glass(image, card, strength=0.61, tint=(48, 51, 59), radius=round(25 * u))
            centered_label(image, "ROUND COMPLETE", rect_fraction(width, height, 0.31, 0.405, 0.69, 0.465), 0.83 * u)
            centered_label(image, f"{self.score} SIDE PALM HITS", rect_fraction(width, height, 0.31, 0.47, 0.69, 0.57), 1.02 * u, CYAN)
            replay = self.replay_rect(width, height)
            glass(image, replay, strength=0.65, tint=(70, 82, 91), radius=round(14 * u))
            centered_label(image, "PINCH TO REPLAY", replay, 0.61 * u)

        instruction = "Swing the side of your hand toward the orb  /  Both sides can hit"
        if self.phase == "playing" and not self._valid_plane(now, width, height):
            instruction = "Side palm lost  /  Show the side of your hand to hit again"
        centered_label(image, instruction, rect_fraction(width, height, 0.12, 0.805, 0.88, 0.852),
                       0.51 * u, MUTED)
