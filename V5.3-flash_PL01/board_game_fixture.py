"""Explicit synthetic controller/state exercise on the real UI owner.

The board entry calls ``step(ui_worker, time.perf_counter())`` from its normal
display loop and continues presenting/acknowledging UI packets. Each command
must produce a newer, actually acknowledged packet before the next command.
This verifies game state and display plumbing, not physical gesture or voice.
"""
import copy
from pathlib import Path
import threading
import time

from spatial_build.model import LEVELS, is_legal


CASES = ('enter_and_disabled_place', 'failed_check', 'placement_on',
         'one_placement', 'placement_off', 'drag', 'undo', 'clear',
         'build_target', 'passed_check', 'return')


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


class GameExercise:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent
        self._lock = threading.Lock()
        self._index = 0
        self._pending = None
        self._results = []
        self._status = 'PENDING'
        self._error = None

    @property
    def done(self):
        with self._lock:
            return self._status in ('PASS', 'FAIL')

    def metrics(self):
        with self._lock:
            return dict(status=self._status, done=self._status in ('PASS', 'FAIL'),
                        cases_total=len(LEVELS)*len(CASES),
                        cases_completed=len(self._results),
                        pending_case=self._pending['name'] if self._pending else None,
                        results=copy.deepcopy(self._results), error=self._error,
                        evidence_kind='explicit_ui_owner_fixture',
                        placement_input='synthetic pinch/no-pinch controller samples; no model result injection',
                        display_acceptance='requires caller presentation ACK',
                        real_gesture_acceptance='NOT_RUN', voice_acceptance='NOT_RUN')

    def _fail(self, message):
        with self._lock:
            self._status, self._error = 'FAIL', message
        raise RuntimeError(message)

    def step(self, ui, now):
        """Queue at most one UI-owner action and return without waiting."""
        now = float(now)
        with self._lock:
            status, error, pending = self._status, self._error, self._pending
        if status == 'FAIL':
            raise RuntimeError(error)
        if status == 'PASS':
            return
        current = ui.status()
        if current.get('error') or current.get('command_error'):
            self._fail('game fixture UI owner: ' + str(current.get('error') or current.get('command_error')))
        if pending is not None:
            with self._lock:
                receipt = copy.deepcopy(pending.get('receipt'))
            if receipt and receipt.get('error'):
                self._fail(pending['name'] + ': ' + receipt['error'])
            if now - pending['queued_at'] > 15.:
                self._fail(pending['name'] + ': no completed, presented UI packet within 15 seconds')
            packet = ui.latest()
            if (receipt is None or packet is None
                    or packet.revision <= receipt['previous_revision']
                    or current['displayed_revision'] < packet.revision
                    or current.get('preparing')):
                return
            record = receipt['record']
            record['presented_revision'] = packet.revision
            with self._lock:
                self._results.append(record)
                self._index += 1
                self._pending = None
                if self._index == len(LEVELS)*len(CASES):
                    self._status = 'PASS'
            return
        if not current.get('ready') or current.get('waiting_for_display') or current.get('preparing'):
            return
        level = LEVELS[self._index // len(CASES)]
        case = CASES[self._index % len(CASES)]
        pending = dict(name=level.id + '/' + case, queued_at=now)
        with self._lock:
            self._status, self._pending = 'RUNNING', pending

        def command(app):
            try:
                record = self._apply(app, level, case, time.perf_counter())
                packet = ui.latest()
                receipt = dict(record=record, previous_revision=packet.revision if packet else 0)
            except Exception as exc:
                with self._lock:
                    pending['receipt'] = dict(error=type(exc).__name__ + ': ' + str(exc))
                raise
            with self._lock:
                pending['receipt'] = receipt

        if not ui.command('fixture', command):
            self._fail(pending['name'] + ': UI command was rejected')

    @staticmethod
    def _place(app, x, y, now):
        game, controls = app.spatial, app.controls
        point = game.build_projection.project((x+.5, y+.5, game.board.height_at(x, y)))
        before = game.board.blocks
        # Explicit fixture setup. Times end at the current UI-owner time;
        # no future observation can suppress the real inference stream later.
        controls.reset(require_release=False)
        pressed = ({'label': 'pinch_point', 'confidence': 1., 'center': point},)
        for stamp in (now-.02, now-.01):
            app.handle_events(controls.observe(pressed, app.width, app.height, stamp, app.hit_test), stamp)
            _require(game.board.blocks == before, 'held synthetic pinch placed before release')
        app.handle_events(controls.tick(now-.005), now-.005)
        _require(game.board.blocks == before, 'render tick committed a placement release')
        app.handle_events(controls.observe((), app.width, app.height, now, app.hit_test), now)

    def _apply(self, app, level, case, now):
        game = app.spatial
        goal_order = sorted(level.coords, key=lambda p: (p[2], -p[1], p[0]))
        first = goal_order[0]
        if case == 'enter_and_disabled_place':
            _require(app.stage == 'levels', 'level entry requires the selector')
            _require(app._button('level_' + level.id, now), 'level button rejected')
            _require(game.level.id == level.id and app.stage == 'build', 'wrong level entered')
            _require(not game.placement_enabled and len(game.board) == 0, 'entry must start empty with placement off')
            self._place(app, 0, 0, now)
            _require(len(game.board) == 0, 'placement off added a block')
        elif case == 'failed_check':
            before = game.board.blocks
            app._button('check', now)
            result = game.check_result
            _require(result is not None and not result.passed, 'empty construction passed')
            _require(bool(result.front_differences or result.left_differences or result.top_differences),
                     'failed check did not identify projection differences')
            _require(app.stage == 'build' and game.board.blocks == before, 'failed check changed construction')
        elif case == 'placement_on':
            app._button('place_toggle', now)
            _require(game.placement_enabled, 'placement did not enable')
        elif case == 'one_placement':
            self._place(app, first[0], first[1], now)
            _require(game.board.coords == (first,), 'one synthetic confirmed pinch/release did not add exactly one block')
        elif case == 'placement_off':
            app._button('place_toggle', now)
            _require(not game.placement_enabled, 'placement did not disable')
            self._place(app, 1, 0, now)
            _require(game.board.coords == (first,), 'placement off added a block')
        elif case == 'drag':
            block = game.board.top_at(first[0], first[1])
            _require(block is not None and game.scene is not None, 'drag needs a prepared existing block')
            point = game.scene.project((.5, .5, 0))
            app.handle_events((dict(kind='drag_start', target=('move', block.id), point=point),
                               dict(kind='drag_end')), now)
            _require(game.board.coords == ((0, 0, 0),), 'drag did not move the block')
            _require(game.board.top_at(0, 0).id == block.id, 'drag replaced the stable block ID')
        elif case == 'undo':
            app._button('undo', now)
            _require(game.board.coords == (first,), 'undo did not restore the dragged block')
        elif case == 'clear':
            app._button('reset', now)
            _require(len(game.board) == 0 and not game.placement_enabled, 'clear did not empty and disable placement')
        elif case == 'build_target':
            app._button('place_toggle', now)
            for x, y, _ in goal_order:
                self._place(app, x, y, now)
            _require(set(game.board.coords) == set(level.coords), 'target placement coordinates differ')
            _require(is_legal(game.board.blocks), 'target construction is illegal')
        elif case == 'passed_check':
            before = game.board.blocks
            app._button('check', now)
            _require(game.check_result is not None and game.check_result.passed, 'target did not pass all projections')
            _require(game.board.blocks == before and app.stage == 'build', 'pass changed IDs or left the editable game')
        elif case == 'return':
            _require(app.go_back(), 'return was rejected')
            _require(app.stage == 'levels' and not game.placement_enabled, 'return did not restore the selector with placement off')
        return dict(level=level.id, case=case, status='PASS', stage=app.stage,
                    blocks=len(game.board), coordinates=[list(coord) for coord in game.board.coords],
                    placement_enabled=game.placement_enabled,
                    projection_passed=game.check_result.passed if game.check_result is not None else None)
