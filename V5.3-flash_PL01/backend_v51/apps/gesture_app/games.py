"""Small 2-D games controlled by YOLO pinch and palm detections."""

from __future__ import annotations

import math
import random
import time

import cv2
import numpy as np

from .core import Detection
from .palm_ball import PalmBounce
from .block_builder import BlockBuilder
from .window_resize import CornerResize, WindowView
from .spatial_ui import CYAN, INK, LAVENDER, MUTED, blend_background, centered_label, fill_round, glass, glow, label, rect_fraction, unit


def text(image: np.ndarray, label: str, x: int, y: int, scale: float = 0.65,
         color: tuple[int, int, int] = INK, thickness: int = 1) -> None:
    cv2.putText(image, label, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def button(image: np.ndarray, rect: tuple[int, int, int, int], label: str,
           color: tuple[int, int, int] = (55, 64, 73)) -> None:
    x1, y1, x2, y2 = rect
    scale = unit(image.shape[1], image.shape[0])
    glass(image, rect, strength=0.57, tint=color, radius=max(9, round(16 * scale)))
    centered_label(image, label, rect, 0.65 * scale, INK, 1)


def inside(point: tuple[float, float], rect: tuple[int, int, int, int]) -> bool:
    return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def close_button(image: np.ndarray, rect: tuple[int, int, int, int], active: bool) -> None:
    """A glass close control; the same rectangle is used for pinch selection."""
    x1, y1, x2, y2 = rect
    u = unit(image.shape[1], image.shape[0])
    glass(image, rect, strength=0.65 if active else 0.42,
          tint=(76, 78, 84) if active else (49, 54, 60), radius=(x2 - x1) // 2)
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    arm = max(6, round(8 * u))
    thickness = max(2, round(2 * u))
    cv2.line(image, (cx - arm, cy - arm), (cx + arm, cy + arm), INK, thickness, cv2.LINE_AA)
    cv2.line(image, (cx + arm, cy - arm), (cx - arm, cy + arm), INK, thickness, cv2.LINE_AA)


class TicTacToe:
    name = "Tic Tac Toe"

    def __init__(self) -> None:
        self.board = [" "] * 9
        self.message = "You are X. Pinch a square."
        self.finished = False

    @staticmethod
    def winner(board: list[str]) -> str | None:
        for a, b, c in ((0, 1, 2), (3, 4, 5), (6, 7, 8),
                        (0, 3, 6), (1, 4, 7), (2, 5, 8),
                        (0, 4, 8), (2, 4, 6)):
            if board[a] != " " and board[a] == board[b] == board[c]:
                return board[a]
        if " " not in board:
            return "draw"
        return None

    def _computer_move(self) -> None:
        for token in ("O", "X"):
            for index in range(9):
                if self.board[index] != " ":
                    continue
                candidate = self.board.copy()
                candidate[index] = token
                if self.winner(candidate) == token:
                    self.board[index] = "O"
                    return
        for index in (4, 0, 2, 6, 8, 1, 3, 5, 7):
            if self.board[index] == " ":
                self.board[index] = "O"
                return

    @staticmethod
    def board_rect(width: int, height: int) -> tuple[int, int, int, int]:
        side = min(round(width * 0.36), round(height * 0.52))
        side = max(90, side)
        left = (width - side) // 2
        top = round(height * 0.29)
        return left, top, left + side, top + side

    @classmethod
    def window_rect(cls, width: int, height: int) -> tuple[int, int, int, int]:
        x1, y1, x2, y2 = cls.board_rect(width, height)
        u = unit(width, height)
        return (x1 - round(45 * u), y1 - round(72 * u),
                x2 + round(45 * u), y2 + round(32 * u))

    def hit_target(self, point: tuple[float, float], width: int, height: int):
        if self.finished:
            return "replay"
        x1, y1, x2, y2 = self.board_rect(width, height)
        if not inside(point, (x1, y1, x2, y2)):
            return None
        col = min(2, int((point[0] - x1) / ((x2 - x1) / 3)))
        row = min(2, int((point[1] - y1) / ((y2 - y1) / 3)))
        return row * 3 + col

    def click(self, point: tuple[float, float], width: int, height: int) -> bool:
        index = self.hit_target(point, width, height)
        if index == "replay":
            self.__init__()
            return True
        if index is None:
            return False
        if self.board[index] != " ":
            self.message = "Occupied square. Choose another."
            return False
        self.board[index] = "X"
        if self.winner(self.board) is None:
            self._computer_move()
        winner = self.winner(self.board)
        if winner:
            self.finished = True
            self.message = {"X": "You win! Pinch to replay.", "O": "Computer wins. Pinch to replay.",
                            "draw": "Draw. Pinch to replay."}[winner]
        else:
            self.message = "Your turn: pinch an empty square."
        return True

    def render(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        x1, y1, x2, y2 = self.board_rect(width, height)
        side = x2 - x1
        u = unit(width, height)
        glass(image, self.window_rect(width, height),
              strength=0.42, tint=(48, 51, 56), radius=round(26 * u))
        text(image, "TIC TAC TOE", x1, y1 - round(54 * u), 0.82 * u, INK)
        text(image, self.message, x1, y1 - round(24 * u), 0.66 * u, MUTED)
        for index in range(9):
            col, row = index % 3, index // 3
            gap = max(3, round(5 * u))
            rect = (x1 + col * side // 3 + gap, y1 + row * side // 3 + gap,
                    x1 + (col + 1) * side // 3 - gap, y1 + (row + 1) * side // 3 - gap)
            fill_round(image, rect, (50, 55, 62), 0.36, radius=round(14 * u))
        for i in (1, 2):
            cv2.line(image, (x1 + side * i // 3, y1), (x1 + side * i // 3, y2), (165, 181, 194), 1, cv2.LINE_AA)
            cv2.line(image, (x1, y1 + side * i // 3), (x2, y1 + side * i // 3), (165, 181, 194), 1, cv2.LINE_AA)
        for index, mark in enumerate(self.board):
            if mark == " ":
                continue
            col, row = index % 3, index // 3
            center = (x1 + round((col + 0.5) * side / 3), y1 + round((row + 0.5) * side / 3))
            radius = max(10, side // 11)
            if mark == "X":
                cv2.line(image, (center[0] - radius, center[1] - radius),
                         (center[0] + radius, center[1] + radius), CYAN, max(2, round(5 * u)), cv2.LINE_AA)
                cv2.line(image, (center[0] + radius, center[1] - radius),
                         (center[0] - radius, center[1] + radius), CYAN, max(2, round(5 * u)), cv2.LINE_AA)
            else:
                cv2.circle(image, center, radius, LAVENDER, max(2, round(5 * u)), cv2.LINE_AA)


class MemoryMatch:
    name = "Memory Match"

    def __init__(self) -> None:
        self.cards = list("AABBCCDDEEFF")
        random.shuffle(self.cards)
        self.revealed: list[int] = []
        self.matched: set[int] = set()
        self.hide_at = 0.0
        self.turns = 0

    @staticmethod
    def window_rect(width: int, height: int) -> tuple[int, int, int, int]:
        return rect_fraction(width, height, 0.26, 0.19, 0.74, 0.83)

    @staticmethod
    def grid(width: int, height: int) -> tuple[int, int, int, int, int]:
        gap = max(8, round(width * 0.013))
        cell = min(round(width * 0.083), round(height * 0.14))
        cell = max(25, cell)
        grid_w = cell * 4 + gap * 3
        left = (width - grid_w) // 2
        return left, round(height * 0.33), cell, gap, grid_w

    def tick(self) -> None:
        if self.hide_at and time.perf_counter() >= self.hide_at:
            self.revealed.clear()
            self.hide_at = 0.0

    def hit_target(self, point: tuple[float, float], width: int, height: int):
        self.tick()
        if len(self.matched) == 12:
            return "replay"
        if self.hide_at:
            return None
        left, top, cell, gap, _ = self.grid(width, height)
        for index in range(12):
            col, row = index % 4, index // 4
            rect = (left + col * (cell + gap), top + row * (cell + gap),
                    left + col * (cell + gap) + cell, top + row * (cell + gap) + cell)
            if not inside(point, rect) or index in self.matched or index in self.revealed:
                continue
            return index
        return None

    def click(self, point: tuple[float, float], width: int, height: int) -> bool:
        index = self.hit_target(point, width, height)
        if index == "replay":
            self.__init__()
            return True
        if index is None:
            return False
        self.revealed.append(index)
        if len(self.revealed) == 2:
            self.turns += 1
            if self.cards[self.revealed[0]] == self.cards[self.revealed[1]]:
                self.matched.update(self.revealed)
                self.revealed.clear()
            else:
                self.hide_at = time.perf_counter() + 0.85
        return True

    def render(self, image: np.ndarray) -> None:
        self.tick()
        height, width = image.shape[:2]
        u = unit(width, height)
        left, top, cell, gap, _ = self.grid(width, height)
        glass(image, self.window_rect(width, height),
              strength=0.43, tint=(43, 47, 54), radius=round(27 * u))
        text(image, f"MATCH THE PAIRS   /   {len(self.matched) // 2} OF 6",
             round(width * 0.31), round(height * 0.28), 0.75 * u)
        text(image, f"{self.turns} turns", round(width * 0.62), round(height * 0.28), 0.60 * u, MUTED)
        if len(self.matched) == 12:
            text(image, "Complete  /  Pinch to replay", round(width * 0.36), round(height * 0.79), 0.63 * u)
        for index in range(12):
            col, row = index % 4, index // 4
            x = left + col * (cell + gap)
            y = top + row * (cell + gap)
            visible = index in self.matched or index in self.revealed
            color = (76, 124, 102) if index in self.matched else ((97, 92, 116) if visible else (57, 60, 68))
            glass(image, (x, y, x + cell, y + cell), strength=0.62,
                  tint=color, radius=max(8, round(13 * u)))
            label = self.cards[index] if visible else "?"
            centered_label(image, label, (x, y, x + cell, y + cell), 1.0 * u,
                           CYAN if index in self.matched else INK, 2)


class GameHub:
    """One fresh pinch hit clicks; one fresh no-pinch result unlocks the latch."""

    modes = ("zoom", "tic_tac_toe", "memory_match", "palm_bounce", "block_builder")

    def __init__(self, mode: str = "menu") -> None:
        self._trace = None
        self.mode = mode
        self.game = None
        self.pointer: tuple[float, float] | None = None
        self._candidate: tuple[float, float] | None = None
        self._candidate_target = None
        self._stable = 0
        self._missing = 1
        self._latched = False
        self._views: dict[tuple[str, int, int], WindowView] = {}
        self._resize = CornerResize()
        self._view_mode = mode
        self._pinch_points: list[tuple[float, float]] = []
        self.voice_status = dict(state="disabled", listening=False)
        self._voice_request = None
        if mode != "menu":
            self._new_game()

    def _new_game(self) -> None:
        self.game = {"tic_tac_toe": TicTacToe, "memory_match": MemoryMatch,
                     "palm_bounce": PalmBounce, "block_builder": BlockBuilder}.get(self.mode, lambda: None)()
        if isinstance(self.game, PalmBounce):
            self.game.set_trace(self._trace)

    def set_trace(self, callback) -> None:
        self._trace = callback
        if isinstance(self.game, PalmBounce):
            self.game.set_trace(callback)

    def reset(self) -> None:
        if isinstance(self.game, BlockBuilder):
            self.game.cancel_drag()
        self._resize.reset()
        self._new_game()

    def navigate(self, mode: str) -> None:
        """Move UI state on the display thread and release captured objects."""
        if mode not in ("home", "menu") + self.modes:
            raise ValueError("Unknown application mode")
        if isinstance(self.game, BlockBuilder):
            self.game.cancel_drag()
        self._resize.reset()
        self.pointer = None
        self._pinch_points = []
        self._candidate = self._candidate_target = None
        self._stable = 0
        self._missing = 0
        # A held pinch must not click the newly opened window.
        self._latched = True
        if mode != self.mode:
            self.mode = mode
            self._new_game()

    @staticmethod
    def _apps_rect(width: int, height: int) -> tuple[int, int, int, int]:
        return rect_fraction(width, height, 0.44, 0.885, 0.56, 0.955)

    @staticmethod
    def _voice_rect(width: int, height: int) -> tuple[int, int, int, int]:
        return rect_fraction(width, height, 0.78, 0.89, 0.94, 0.955)

    def _draw_voice(self, image: np.ndarray) -> None:
        height, width = image.shape[:2]
        status = self.voice_status
        title = "CANCEL VOICE" if status.get("listening") else "VOICE"
        button(image, self._voice_rect(width, height), title)
        state = str(status.get("state", "disabled"))[:24]
        policy = str(status.get("policy", ""))[:12]
        command = str(status.get("last_command") or "-")[:12]
        result = str(status.get("result") or "")[:40]
        # Detector HUD and diagnostics occupy the top; keep status below controls.
        fill_round(image, (0, round(height * .965), width, height),
                   (18, 22, 28), .78, radius=1)
        label(image, f"VOICE {state} {policy}  /  LAST: {command} {result}",
              round(width * .025), round(height * .985), .5 * unit(width, height), INK)

    def _base_window_rect(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        if self.mode == "menu":
            return rect_fraction(width, height, 0.18, 0.155, 0.82, 0.85)
        if self.mode == "zoom":
            return rect_fraction(width, height, 0.64, 0.19, 0.92, 0.44)
        if self.game is not None:
            return self.game.window_rect(width, height)
        return None

    def _view(self, width: int, height: int) -> WindowView | None:
        if self._view_mode != self.mode:
            self._resize.reset()
            self._pinch_points = []
            self._view_mode = self.mode
        base = self._base_window_rect(width, height)
        if base is None:
            return None
        key = self.mode, width, height
        if key not in self._views:
            self._views[key] = WindowView(base, width, height)
        return self._views[key]

    @property
    def resizing(self) -> bool:
        return self._resize.active or self._resize.engaged

    def _window_rect(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        view = self._view(width, height)
        return tuple(round(v) for v in view.rect) if view is not None else None

    def _base_close_rect(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        window = self._base_window_rect(width, height)
        if window is None:
            return None
        u = unit(width, height)
        size, inset = max(36, round(48 * u)), max(7, round(10 * u))
        _, y1, x2, _ = window
        return x2 - inset - size, y1 + inset, x2 - inset, y1 + inset + size

    def _close_rect(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        base = self._base_close_rect(width, height)
        view = self._view(width, height)
        if base is None or view is None:
            return None
        x1, y1 = view.to_screen(base[:2])
        x2, y2 = view.to_screen(base[2:])
        return round(x1), round(y1), round(x2), round(y2)

    def _go_back(self) -> None:
        self.navigate("home" if self.mode in ("home", "menu") else "menu")

    def _menu_options(self, width: int, height: int) -> list[tuple[str, str, tuple[int, int, int, int]]]:
        labels = (("zoom", "Two-Palm Zoom"), ("tic_tac_toe", "Tic Tac Toe"),
                  ("memory_match", "Memory Match"), ("palm_bounce", "Side Palm Ball"),
                  ("block_builder", "Block Builder"))
        rects = (rect_fraction(width, height, 0.23, 0.36, 0.48, 0.49),
                 rect_fraction(width, height, 0.52, 0.36, 0.77, 0.49),
                 rect_fraction(width, height, 0.23, 0.52, 0.48, 0.65),
                 rect_fraction(width, height, 0.52, 0.52, 0.77, 0.65),
                 rect_fraction(width, height, 0.23, 0.68, 0.77, 0.81))
        return [(mode, name, rect) for (mode, name), rect in zip(labels, rects)]

    def _selection_target(self, point, width, height, view):
        if inside(point, self._voice_rect(width, height)):
            return self.mode, "voice"
        close = self._close_rect(width, height)
        if close is not None and inside(point, close):
            return self.mode, "close"
        if self.mode == "home":
            return ("home", "apps") if inside(point, self._apps_rect(width, height)) else None
        local = view.to_local(point)
        if self.mode == "menu":
            return next((("menu", mode) for mode, _, rect in self._menu_options(width, height)
                         if inside(local, rect)), None)
        if self.game is not None:
            target = self.game.hit_target(local, width, height)
            return (self.mode, target) if target is not None else None
        return None

    def observe(self, detections: list[Detection], width: int, height: int,
                image: np.ndarray | None = None, source_now: float | None = None,
                now: float | None = None, trace_context=None) -> bool:
        view = self._view(width, height)
        pinches = sorted((d for d in detections if d.label == "pinch_point"),
                         key=lambda d: d.confidence, reverse=True)
        points = [d.center for d in pinches]
        self._pinch_points = points[:2]
        consumed = self._resize.update(points, view, trace=self._trace) if view is not None else len(points) >= 2
        if self.mode == "palm_bounce" and self.game is not None:
            if consumed:
                # Changing the viewport is not a physical palm strike.
                self.game.observe([], width, height, now=now, source_now=source_now,
                                  trace_context=trace_context)
            else:
                logical = [Detection(d.label, d.confidence, view.map_box_to_local(d.box))
                           for d in detections]
                if image is None:
                    scene = None
                elif view.scale == 1.0 and view.center == view._base_center:
                    # Observation only reads this owned source snapshot.
                    view._check_image(image)
                    scene = image
                else:
                    scene = view.virtual_scene(image)
                self.game.observe(logical, width, height, image=scene,
                                  now=now, source_now=source_now, trace_context=trace_context)
        if isinstance(self.game, BlockBuilder):
            close = self._close_rect(width, height)
            control = bool(points and (inside(points[0], self._voice_rect(width, height))
                           or (close is not None and inside(points[0], close))))
            if consumed or control:
                self.game.cancel_drag()
            elif self.game.dragging or not points or not self._latched:
                logical = [Detection(d.label, d.confidence, view.map_box_to_local(d.box))
                           for d in detections]
                if self.game.observe(logical, width, height, now=now):
                    self.pointer = points[0] if points else None
                    self._candidate = self._candidate_target = None
                    self._stable = 0
                    self._latched = bool(points) and (self._latched or self.game.dragging)
                    self._missing = 0 if points else self._missing + 1
                    return False
        if consumed:
            if self._trace is not None:
                self._trace(dict(kind='app_refusal', reason='dual_pinch_or_resize_consumed',
                                 pinch_count=len(pinches), latched_before=self._latched))
            self.pointer = None
            self._candidate = None
            self._candidate_target = None
            self._stable = 0
            if self._trace is not None and self._latched and not pinches:
                self._trace(dict(kind='app_release', reason='fresh_no_pinch_resize_release'))
            self._latched = bool(pinches)
            self._missing = 0 if pinches else 1
            if not pinches:
                self._pinch_points = []
            return False
        if not pinches:
            if self._trace is not None:
                self._trace(dict(kind='app_refusal', reason='no_pinch',
                                 labels=[d.label for d in detections]))
                if self._latched:
                    self._trace(dict(kind='app_release', reason='fresh_no_pinch'))
            self._missing = 1
            self.pointer = None
            self._candidate = None
            self._candidate_target = None
            self._stable = 0
            self._latched = False
            return False
        point = max(pinches, key=lambda d: d.confidence).center
        self.pointer = point
        self._missing = 0
        target = self._selection_target(point, width, height, view)
        self._candidate = point
        self._stable = 1
        self._candidate_target = target
        if self._trace is not None:
            selection = dict(application=self.mode, point=point,
                             local_point=view.to_local(point) if view is not None else point,
                             target=target, target_hit=target is not None,
                             latched=self._latched, close_rect=self._close_rect(width, height),
                             window_rect=self._window_rect(width, height),
                             window_scale=view.scale if view is not None else None)
        if target is None or self._latched:
            if self._trace is not None:
                self._trace(dict(selection, kind='app_refusal',
                    reason='not_in_hit_region' if target is None else 'click_latch_held'))
            return False
        close = self._close_rect(width, height)
        if inside(point, self._voice_rect(width, height)):
            self._latched = True
            self._voice_request = "cancel" if self.voice_status.get("listening") else "start"
            if self._trace is not None:
                self._trace(dict(selection, kind='app_action', action='voice',
                                 request=self._voice_request))
            return True
        if close is not None and inside(point, close):
            self._latched = True
            if self._trace is not None:
                self._trace(dict(selection, kind='app_action', action='close'))
            self._go_back()
            return True
        if self.mode == "home" and inside(point, self._apps_rect(width, height)):
            self._latched = True
            if self._trace is not None:
                self._trace(dict(selection, kind='app_action', action='apps'))
            self.mode = "menu"
            return True
        if self.mode == "menu":
            for mode, _, rect in self._menu_options(width, height):
                if inside(view.to_local(point), rect):
                    self._latched = True
                    if self._trace is not None:
                        self._trace(dict(selection, kind='app_action', action='open_app',
                                         opened_application=mode))
                    self.mode = mode
                    self._new_game()
                    return True
        elif self.game is not None:
            if self.game.click(view.to_local(point), width, height):
                self._latched = True
                if self._trace is not None:
                    self._trace(dict(selection, kind='app_action', action='game_click'))
                return True
        if self._trace is not None:
            self._trace(dict(selection, kind='app_refusal', reason='game_click_rejected'))
        return False

    def _draw_window(self, image: np.ndarray, zoom: float,
                     pointer: tuple[float, float] | None) -> None:
        height, width = image.shape[:2]
        u = unit(width, height)

        if self.mode == "zoom":
            glass(image, self._base_window_rect(width, height),
                  strength=0.43, tint=(49, 55, 59), radius=round(25 * u))
            label(image, "TWO-PALM ZOOM", round(width * 0.67), round(height * 0.25),
                  0.72 * u)
            label(image, f"{zoom:.2f}x", round(width * 0.67), round(height * 0.335),
                  1.65 * u, CYAN, 2)
            label(image, "Move palms apart or together", round(width * 0.67),
                  round(height * 0.397), 0.47 * u, MUTED)
        else:
            if self.mode == "menu":
                glass(image, self._base_window_rect(width, height),
                      strength=0.48, tint=(47, 51, 56), radius=round(30 * u))
                label(image, "Choose your space", round(width * 0.23), round(height * 0.245),
                      1.36 * u, INK)
                label(image, "Pinch a card to enter", round(width * 0.23), round(height * 0.295),
                      0.65 * u, MUTED)
                descriptions = {"zoom": "MOVE  /  SCALE", "tic_tac_toe": "PLAY  /  STRATEGY",
                                "memory_match": "FOCUS  /  REMEMBER", "palm_bounce": "SWING  /  HIT",
                                "block_builder": "BUILD  /  STACK"}
                icons = {"zoom": "<>  ", "tic_tac_toe": "#", "memory_match": "::",
                         "palm_bounce": "o/", "block_builder": "+"}
                for mode, name, rect in self._menu_options(width, height):
                    active = pointer is not None and inside(pointer, rect)
                    glass(image, rect, strength=0.60 if active else 0.43,
                          tint=(76, 78, 84) if active else (49, 54, 60), radius=round(18 * u))
                    x1, y1, x2, y2 = rect
                    icon_rect = (x1 + round(15 * u), y1 + round(17 * u),
                                 x1 + round(74 * u), y2 - round(17 * u))
                    fill_round(image, icon_rect, (91, 104, 115), 0.55, radius=round(13 * u))
                    centered_label(image, icons[mode], icon_rect, 0.94 * u, CYAN, 1)
                    short_name = "Palm Zoom" if width < 900 and mode == "zoom" else name
                    label(image, short_name, x1 + round(91 * u), y1 + round(45 * u), 0.76 * u, INK)
                    label(image, descriptions[mode], x1 + round(91 * u),
                          y1 + round(68 * u), 0.49 * u, MUTED)
            else:
                self.game.render(image)

        close = self._base_close_rect(width, height)
        if close is not None:
            close_button(image, close, pointer is not None and inside(pointer, close))

    def _draw_resize_handles(self, image: np.ndarray, view: WindowView) -> None:
        x1, y1, x2, y2 = (round(v) for v in view.rect)
        u = unit(image.shape[1], image.shape[0])
        arm = max(10, round(17 * u * min(1.0, view.scale)))
        color = CYAN if self.resizing else MUTED
        for corner, horizontal, vertical in (((x1, y2), 1, -1), ((x2, y1), -1, 1)):
            if self.resizing:
                glow(image, corner, max(8, round(12 * u)), CYAN)
            cv2.line(image, corner, (corner[0] + horizontal * arm, corner[1]), color, 2, cv2.LINE_AA)
            cv2.line(image, corner, (corner[0], corner[1] + vertical * arm), color, 2, cv2.LINE_AA)

    def render(self, image: np.ndarray, fps: float, zoom: float) -> np.ndarray:
        height, width = image.shape[:2]
        u = unit(width, height)
        view = self._view(width, height)
        if view is None:
            button(image, self._apps_rect(width, height), "APPS")
            self._draw_voice(image)
            return image

        blend_background(image)
        local_pointer = view.to_local(self.pointer) if self.pointer is not None else None
        base = view.base_rect
        base_center = ((base[0] + base[2]) / 2, (base[1] + base[3]) / 2)
        if abs(view.scale - 1.0) < 1e-6 and math.dist(view.center, base_center) < 1e-6:
            self._draw_window(image, zoom, local_pointer)
        else:
            workspace = getattr(self, '_window_workspace', None)
            if workspace is None or workspace.shape != image.shape:
                workspace = self._window_workspace = np.empty_like(image)
            virtual = view.virtual_scene(image, destination=workspace, roi_only=True)
            x1, y1, x2, y2 = view.base_rect
            before = virtual[y1:y2, x1:x2].copy()
            self._draw_window(virtual, zoom, local_pointer)
            view.composite(image, before, virtual)
        self._draw_resize_handles(image, view)

        bottom = rect_fraction(width, height, 0.31, 0.895, 0.69, 0.955)
        glass(image, bottom, strength=0.38, tint=(42, 48, 53), radius=round(18 * u))
        if self._resize.active:
            hint = f"RESIZE  {view.scale * 100:.0f}%    /    RELEASE TO FINISH"
        else:
            hint = ("PINCH / MOVE / RELEASE" if self.mode == "block_builder" else
                    "SIDE PALM TO HIT    /    PINCH CORNERS TO RESIZE" if self.mode == "palm_bounce"
                    else "PINCH TO SELECT    /    PINCH CORNERS TO RESIZE")
        centered_label(image, hint, bottom,
                       0.50 * u, MUTED)
        self._draw_voice(image)
        return image

    def draw_pointer(self, image: np.ndarray) -> None:
        points = self._pinch_points if len(self._pinch_points) >= 2 else (
            [self.pointer] if self.pointer is not None else [])
        if not points:
            return
        height, width = image.shape[:2]
        u = unit(width, height)
        radius = max(11, round(18 * u))
        for point in points:
            center = (round(point[0]), round(point[1]))
            glow(image, center, radius, CYAN)
            cv2.circle(image, center, radius, CYAN, max(2, round(2 * u)), cv2.LINE_AA)
            cv2.circle(image, center, max(3, round(5 * u)), INK, -1, cv2.LINE_AA)
