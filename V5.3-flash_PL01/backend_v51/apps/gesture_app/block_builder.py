"""Small isometric construction toy using existing pinch detections.

Only fresh inference observations advance the grab/release state. Rendering is
independent of inference and uses a fixed 5 by 4 grid, with no 3-D engine.
"""

from __future__ import annotations

import math
import time
from typing import Tuple

import cv2
import numpy as np

from .core import Detection
from .pseudo3d import draw_block
from .spatial_ui import INK, MUTED, centered_label, fill_round, label, unit


Point = Tuple[float, float]
Cell = Tuple[int, int]
Rect = Tuple[int, int, int, int]


def _inside(point: Point, rect: Rect) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


class BlockBuilder:
    """Pinch a brick, carry it, release over a tile to place or stack it.

    ``board[(column, row)]`` stores color indices from bottom to top. Empty
    columns are absent. While moving an existing brick its top is removed,
    then either committed at the destination or restored by ``cancel_drag``.
    """

    name = "Block Builder"
    board_size = (5, 4)
    max_height = 5
    max_blocks = 24
    # A single missed inference followed by a 0.5-1 s update must not drop a
    # carried brick. This grace is local to this game, not a global gesture TTL.
    pinch_loss_grace_s = 1.25
    tracking_timeout_s = 3.0
    colors = ((88, 151, 238), (227, 171, 83), (155, 205, 119), (209, 133, 188))
    color_names = ("CORAL", "SKY", "MINT", "LILAC")

    def __init__(self) -> None:
        self.board: dict[Cell, list[int]] = {}
        self.message = "Pinch, move, release"
        self._history: list[dict[Cell, list[int]]] = []
        self._drag_color: int | None = None
        self._drag_source: Cell | None = None
        self._drag_before: dict[Cell, list[int]] | None = None
        self._drag_point: Point | None = None
        self._target: Cell | None = None
        self._candidate: tuple[str, object] | None = None
        self._candidate_count = 0
        self._candidate_point: Point | None = None
        self._pinch_lost_at: float | None = None
        self._missing = 0
        self._blocked_until_release = False
        self._last_observation = None

    @property
    def dragging(self) -> bool:
        return self._drag_color is not None

    @property
    def block_count(self) -> int:
        return sum(len(stack) for stack in self.board.values())

    @staticmethod
    def window_rect(width: int, height: int) -> Rect:
        return round(width * .12), round(height * .16), round(width * .88), round(height * .86)

    @staticmethod
    def _geometry(width: int, height: int) -> tuple[float, float, int, int, int]:
        scale = unit(width, height)
        return (width * .61, height * .60, max(13, round(52 * scale)),
                max(7, round(25 * scale)), max(8, round(28 * scale)))

    @classmethod
    def tile_top_center(cls, col: int, row: int, width: int, height: int,
                        level: int = 0) -> Point:
        ox, oy, hw, hd, brick_height = cls._geometry(width, height)
        return ox + (col - row - .5) * hw, oy + (col + row - 3.5) * hd - level * brick_height

    @staticmethod
    def palette_rects(width: int, height: int) -> list[Rect]:
        scale = unit(width, height)
        left, right = round(width * .155), round(width * .31)
        top = round(height * .315)
        card_height, gap = max(30, round(64 * scale)), max(7, round(15 * scale))
        return [(left, top + i * (card_height + gap), right, top + i * (card_height + gap) + card_height)
                for i in range(4)]

    @staticmethod
    def toolbar_rects(width: int, height: int) -> dict[str, Rect]:
        u = unit(width, height)
        y1, y2 = round(height * .785), round(height * .785) + max(24, round(34 * u))
        return {"undo": (round(width * .55), y1, round(width * .65), y2),
                "clear": (round(width * .675), y1, round(width * .775), y2)}

    @classmethod
    def _diamond(cls, center: Point, width: int, height: int, inset: int = 0) -> np.ndarray:
        _, _, hw, hd, _ = cls._geometry(width, height)
        x, y = center
        return np.asarray(((round(x), round(y - hd + inset)),
                           (round(x + hw - inset), round(y)),
                           (round(x), round(y + hd - inset)),
                           (round(x - hw + inset), round(y))), np.int32)

    @classmethod
    def _contains_top(cls, point: Point, center: Point, width: int, height: int) -> bool:
        _, _, hw, hd, _ = cls._geometry(width, height)
        return abs(point[0] - center[0]) / hw + abs(point[1] - center[1]) / hd <= 1.12

    def _ordered_cells(self) -> list[Cell]:
        return sorted(((col, row) for col in range(self.board_size[0])
                       for row in range(self.board_size[1])), key=lambda cell: (sum(cell), cell[1]))

    def _surface_cell(self, point: Point, width: int, height: int,
                      occupied_only: bool = False) -> Cell | None:
        # The frontmost visible top face wins when raised columns overlap.
        for cell in reversed(self._ordered_cells()):
            stack = self.board.get(cell, [])
            if not stack:
                continue
            center = self.tile_top_center(*cell, width, height, len(stack))
            if self._contains_top(point, center, width, height):
                return cell
            # A front column's side faces cover surfaces behind it. Do not
            # reach through a visible side to grab an occluded rear brick.
            _, _, hw, hd, brick_height = self._geometry(width, height)
            x, y = center
            bottom = y + len(stack) * brick_height
            silhouette = np.asarray(((round(x), round(y - hd)),
                                     (round(x + hw), round(y)),
                                     (round(x + hw), round(bottom)),
                                     (round(x), round(bottom + hd)),
                                     (round(x - hw), round(bottom)),
                                     (round(x - hw), round(y))), np.int32)
            if cv2.pointPolygonTest(silhouette, point, False) >= 0:
                return None
        if not occupied_only:
            for cell in reversed(self._ordered_cells()):
                if not self.board.get(cell) and self._contains_top(
                        point, self.tile_top_center(*cell, width, height), width, height):
                    return cell
        return None

    def _copy_board(self) -> dict[Cell, list[int]]:
        return {cell: stack.copy() for cell, stack in self.board.items() if stack}

    def _remember(self, before: dict[Cell, list[int]]) -> None:
        self._history.append(before)
        del self._history[:-32]

    def _reset_candidate(self) -> None:
        self._candidate = None
        self._candidate_count = 0
        self._candidate_point = None

    def cancel_drag(self) -> bool:
        """Restore a lifted brick; consume a held pinch until a fresh release."""
        had_capture = self.dragging or self._candidate is not None
        if self.dragging and self._drag_before is not None:
            self.board = {cell: stack.copy() for cell, stack in self._drag_before.items()}
        self._drag_color = None
        self._drag_source = None
        self._drag_before = None
        self._drag_point = None
        self._target = None
        self._pinch_lost_at = None
        self._reset_candidate()
        self._missing = 0
        if had_capture:
            self._blocked_until_release = True
        return had_capture

    def _finish_drag(self, width: int, height: int) -> None:
        target = self._surface_cell(self._drag_point, width, height) if self._drag_point else None
        if target is None or len(self.board.get(target, [])) >= self.max_height:
            self.cancel_drag()
            self.message = "Move over a free tile"
            return
        color, before = self._drag_color, self._drag_before
        self.board.setdefault(target, []).append(color)
        if before != self.board:
            self._remember(before)
        self._drag_color = None
        self._drag_source = None
        self._drag_before = None
        self._drag_point = None
        self._target = None
        self._pinch_lost_at = None
        self._reset_candidate()
        self._missing = 0
        self.message = "Pinch, move, release"

    def expire_tracking(self, now: float) -> None:
        """An inference outage cancels capture; it never places a stale brick."""
        if (self._last_observation is not None
                and now - self._last_observation > self.tracking_timeout_s):
            if self.cancel_drag():
                self.message = "Tracking paused / grab again"

    def hit_target(self, point: Point, width: int, height: int):
        if self.dragging or self._blocked_until_release:
            return None
        return next((name for name, rect in self.toolbar_rects(width, height).items()
                     if _inside(point, rect)), None)

    def observe(self, detections: list[Detection], width: int, height: int,
                now: float | None = None, image: np.ndarray | None = None) -> bool:
        """Advance once for each fresh model result; return consumed gesture."""
        now = time.perf_counter() if now is None else float(now)
        self.expire_tracking(now)
        self._last_observation = now
        # WindowView maps valid screen points into logical coordinates. After
        # shrinking a window, a held pinch may lie outside the logical frame;
        # it still means "held", so retain it for invalid-drop/jump handling.
        pinches = [d for d in detections if d.label == "pinch_point"
                   and all(math.isfinite(value) for value in d.center)]
        if self._blocked_until_release:
            self._missing = 0 if pinches else self._missing + 1
            if self._missing >= 2:
                self._blocked_until_release = False
                self._missing = 0
            return True
        if not pinches:
            self._missing += 1
            if self.dragging:
                # Start on a fresh missing result, not on a skipped/rendered
                # frame. Keep the brick frozen at its last tracked position.
                if self._pinch_lost_at is None:
                    self._pinch_lost_at = now
                if self._missing >= 2 and now - self._pinch_lost_at >= self.pinch_loss_grace_s:
                    self._finish_drag(width, height)
                return True
            was_pending = self._candidate is not None
            self._reset_candidate()
            return was_pending
        if (self.dragging and self._pinch_lost_at is not None
                and now - self._pinch_lost_at >= self.pinch_loss_grace_s):
            # A result arriving after the deadline cannot resurrect a carry
            # merely because no inference happened at the exact expiry time.
            self._finish_drag(width, height)
            self._blocked_until_release = True
            return True
        self._missing = 0
        reference = self._drag_point if self.dragging else self._candidate_point
        pinch = (min(pinches, key=lambda d: math.dist(d.center, reference)) if reference is not None
                 else max(pinches, key=lambda d: d.confidence))
        point = pinch.center
        if self.dragging:
            if math.dist(point, self._drag_point) > .45 * math.hypot(width, height):
                self.cancel_drag()
                self.message = "Grab again"
                return True
            self._drag_point = point
            self._pinch_lost_at = None
            self._target = self._surface_cell(point, width, height)
            self.message = ("Release to place" if self._target is not None
                            and len(self.board.get(self._target, [])) < self.max_height else "Move over a free tile")
            return True
        source: tuple[str, object] | None = None
        for color, rect in enumerate(self.palette_rects(width, height)):
            if _inside(point, rect):
                source = ("palette", color)
                break
        if source is None:
            cell = self._surface_cell(point, width, height, occupied_only=True)
            if cell is not None:
                source = ("board", cell)
        if source is None:
            self._reset_candidate()
            return False
        # A stable source and nearby point in two independent inference results.
        stable = (source == self._candidate and self._candidate_point is not None
                  and math.dist(point, self._candidate_point) <= max(18, 72 * unit(width, height)))
        self._candidate_count = self._candidate_count + 1 if stable else 1
        self._candidate = source
        self._candidate_point = point
        if self._candidate_count < 2:
            return True
        if source[0] == "palette" and self.block_count >= self.max_blocks:
            self.message = "24 blocks  /  Undo or Clear"
            return True
        self._drag_before = self._copy_board()
        if source[0] == "board":
            cell = source[1]
            self._drag_source = cell
            self._drag_color = self.board[cell].pop()
            if not self.board[cell]:
                del self.board[cell]
        else:
            self._drag_source = None
            self._drag_color = int(source[1])
        self._drag_point = point
        self._pinch_lost_at = None
        self._target = None
        self.message = "Release to place"
        self._reset_candidate()
        return True

    def click(self, point: Point, width: int, height: int) -> bool:
        """Toolbar only; grabbing is handled by ``observe``, never by click."""
        if self.dragging or self._blocked_until_release:
            return False
        controls = self.toolbar_rects(width, height)
        if _inside(point, controls["undo"]):
            if self._history:
                self.board = self._history.pop()
                self.message = "Undone"
            return True
        if _inside(point, controls["clear"]):
            if self.board:
                self._remember(self._copy_board())
                self.board.clear()
                self.message = "Board cleared"
            return True
        return False

    def render(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        u = unit(width, height)
        _, _, hw, hd, brick_height = self._geometry(width, height)
        fill_round(image, self.window_rect(width, height), (44, 48, 54), .43,
                   radius=max(12, round(27 * u)))
        label(image, "BLOCK STUDIO", round(width * .155), round(height * .255), .82 * u, INK)
        label(image, "FREE BUILD", round(width * .155), round(height * .292), .48 * u, MUTED)
        label(image, f"{self.block_count + int(self.dragging):02d} / 24", round(width * .765),
              round(height * .255), .62 * u, MUTED)
        for index, rect in enumerate(self.palette_rects(width, height)):
            fill_round(image, rect, (61, 65, 73), .54, radius=max(6, round(13 * u)))
            x1, y1, x2, y2 = rect
            preview = (round(x1 + 45 * u), round((y1 + y2) / 2 - 4 * u))
            draw_block(image, preview, max(12, round(27 * u)), max(7, round(13 * u)),
                       max(7, round(15 * u)), self.colors[index], shadow=True)
            label(image, self.color_names[index], round(x1 + 88 * u), round((y1 + y2) / 2 + 5 * u),
                  .48 * u, INK)

        # A shallow board has two lit side faces; each tile stays a tiny polygon.
        ox, oy, _, _, _ = self._geometry(width, height)
        outline = np.asarray(((round(ox - .5 * hw), round(oy - 4.5 * hd)),
                              (round(ox + 4.5 * hw), round(oy + .5 * hd)),
                              (round(ox + .5 * hw), round(oy + 4.5 * hd)),
                              (round(ox - 4.5 * hw), round(oy - .5 * hd))), np.int32)
        base = max(3, round(8 * u))
        for face, color in (((outline[3], outline[2]), (52, 66, 81)),
                            ((outline[2], outline[1]), (64, 79, 92))):
            a, b = face
            polygon = np.asarray((a, b, b + (0, base), a + (0, base)), np.int32)
            cv2.fillConvexPoly(image, polygon, color, cv2.LINE_AA)
        for col, row in self._ordered_cells():
            center = self.tile_top_center(col, row, width, height)
            polygon = self._diamond(center, width, height, max(1, round(2 * u)))
            color = (88, 99, 109) if (col + row) % 2 else (95, 106, 115)
            cv2.fillConvexPoly(image, polygon, color, cv2.LINE_AA)
            cv2.polylines(image, [polygon], True, (121, 135, 145), 1, cv2.LINE_AA)
        cv2.polylines(image, [outline], True, (170, 182, 190), max(1, round(2 * u)), cv2.LINE_AA)

        for cell in self._ordered_cells():
            stack = self.board.get(cell, [])
            for level, color in enumerate(stack, start=1):
                center = self.tile_top_center(*cell, width, height, level)
                draw_block(image, (round(center[0]), round(center[1])), max(9, hw - round(3 * u)),
                           max(5, hd - round(2 * u)), brick_height, self.colors[color],
                           shadow=level == 1)
        if self.dragging and self._drag_point is not None:
            if self._target is not None:
                current_height = len(self.board.get(self._target, []))
                surface = self.tile_top_center(*self._target, width, height, current_height)
                polygon = self._diamond(surface, width, height)
                valid = current_height < self.max_height
                cv2.polylines(image, [polygon], True, (181, 232, 200) if valid else (130, 139, 242),
                              max(2, round(3 * u)), cv2.LINE_AA)
            draw_block(image, (round(self._drag_point[0]), round(self._drag_point[1] - brick_height)),
                       max(9, hw - round(3 * u)), max(5, hd - round(2 * u)), brick_height,
                       self.colors[self._drag_color], selected=True, shadow=True)
        for name, rect in self.toolbar_rects(width, height).items():
            fill_round(image, rect, (82, 88, 99), .56, radius=max(6, round(10 * u)))
            centered_label(image, name.upper(), rect, .53 * u, INK)
        label(image, self.message, round(width * .155), round(height * .83), .52 * u, MUTED)
