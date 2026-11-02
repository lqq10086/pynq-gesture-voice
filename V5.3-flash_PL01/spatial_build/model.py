"""Small block world and the three authoritative projection masks.

World axes: x runs right across the front of the board; y runs away from
the front; z runs up. FRONT looks from y < 0 toward +y, so image columns
are x=0..4. LEFT looks from x < 0 toward +x, so image columns are
y=3..0: the near/front edge (y=0) is on the RIGHT of the left view.
FRONT and LEFT store rows bottom first: mask[z][image_column]. A renderer
must reverse those rows to display the uppermost row at the top of a screen.
TOP looks from z > 0 toward -z and stores world-coordinate rows y=0..3,
columns x=0..4: mask[y][x]. The spatial hint plane uses those coordinates
directly, without an extra screen-space mirroring or row reversal.

Public construction rules are exactly: integer cells within the world,
one block per cell, a floor/support below every block, and at most 24
blocks. Connectivity and agreement with a hidden target block count are
NOT rules. Matching all three projections is sufficient for a legal answer.
"""

from dataclasses import dataclass
from numbers import Integral
from typing import Iterable, List, Optional, Sequence, Tuple, Union

WIDTH = 5
DEPTH = 4
MAX_HEIGHT = 5
CAPACITY = 24
FRONT_DIRECTION = "Look from y<0 toward +y; image left-to-right is x=0..4."
LEFT_DIRECTION = "Look from x<0 toward +x; image left-to-right is y=3..0."
TOP_DIRECTION = "Look from z>0 toward -z; mask rows are y=0..3, columns x=0..4."
Coord = Tuple[int, int, int]
Mask = Tuple[Tuple[bool, ...], ...]


@dataclass(frozen=True)
class Block:
    id: int
    x: int
    y: int
    z: int

    @property
    def coord(self) -> Coord:
        return self.x, self.y, self.z


Cell = Union[Block, Sequence[int]]


def coordinates(blocks: Iterable[Cell]) -> Tuple[Coord, ...]:
    """Materialize coordinates without silently rounding fractional cells."""
    result = []
    for block in blocks:
        values = block.coord if isinstance(block, Block) else tuple(block)
        if len(values) != 3 or any(isinstance(v, bool) or not isinstance(v, Integral) for v in values):
            raise ValueError("Each block needs three integer coordinates.")
        result.append(tuple(int(v) for v in values))
    return tuple(result)


def legality_errors(blocks: Iterable[Cell]) -> Tuple[str, ...]:
    """Return all rule failures; no projection comparison is performed here."""
    materialized = tuple(blocks)
    try:
        coords = coordinates(materialized)
    except (TypeError, ValueError):
        return ("Coordinates must be integer triples.",)
    errors = []
    if len(coords) > CAPACITY:
        errors.append("Block capacity exceeded.")
    if len(set(coords)) != len(coords):
        errors.append("Overlapping blocks.")
    if any(not (0 <= x < WIDTH and 0 <= y < DEPTH and 0 <= z < MAX_HEIGHT) for x, y, z in coords):
        errors.append("Block outside world bounds.")
    occupied = set(coords)
    if any(z > 0 and (x, y, z - 1) not in occupied for x, y, z in coords):
        errors.append("Unsupported block.")
    ids = [b.id for b in materialized if isinstance(b, Block)]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate block ID.")
    return tuple(errors)


def is_legal(blocks: Iterable[Cell]) -> bool:
    return not legality_errors(blocks)


def _project(blocks: Iterable[Cell], left: bool) -> Mask:
    columns = DEPTH if left else WIDTH
    mask = [[False] * columns for _ in range(MAX_HEIGHT)]
    for x, y, z in coordinates(blocks):
        if not (0 <= x < WIDTH and 0 <= y < DEPTH and 0 <= z < MAX_HEIGHT):
            raise ValueError("Cannot project a block outside world bounds.")
        mask[z][DEPTH - 1 - y if left else x] = True
    return tuple(tuple(row) for row in mask)


def front_projection(blocks: Iterable[Cell]) -> Mask:
    """Return bottom-first z rows, left-to-right x columns, union over y."""
    return _project(blocks, left=False)


def left_projection(blocks: Iterable[Cell]) -> Mask:
    """Return bottom-first z rows, left-to-right y=3..0, union over x."""
    return _project(blocks, left=True)


def top_projection(blocks: Iterable[Cell]) -> Mask:
    """Return world y rows and world x columns, union over height z."""
    mask = [[False] * WIDTH for _ in range(DEPTH)]
    for x, y, z in coordinates(blocks):
        if not (0 <= x < WIDTH and 0 <= y < DEPTH and 0 <= z < MAX_HEIGHT):
            raise ValueError("Cannot project a block outside world bounds.")
        mask[y][x] = True
    return tuple(tuple(row) for row in mask)


def projections(blocks: Iterable[Cell]) -> Tuple[Mask, Mask, Mask]:
    saved = tuple(blocks)
    return front_projection(saved), left_projection(saved), top_projection(saved)


@dataclass(frozen=True)
class Level:
    id: str
    name: str
    coords: Tuple[Coord, ...]

    def __post_init__(self) -> None:
        fixed = coordinates(self.coords)
        errors = legality_errors(fixed)
        if errors:
            raise ValueError("Illegal level target: " + " ".join(errors))
        object.__setattr__(self, "coords", fixed)

    @property
    def blocks(self) -> Tuple[Coord, ...]:
        return self.coords

    @property
    def front(self) -> Mask:
        return front_projection(self.coords)

    @property
    def left(self) -> Mask:
        return left_projection(self.coords)

    @property
    def top(self) -> Mask:
        return top_projection(self.coords)


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    legal: bool
    front_differences: Tuple[Tuple[int, int], ...]
    left_differences: Tuple[Tuple[int, int], ...]
    top_differences: Tuple[Tuple[int, int], ...]
    message: str
    errors: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.passed


def judge(blocks: Iterable[Cell], target: Union[Level, Iterable[Cell]]) -> CheckResult:
    """Accept ALL legal structures with the target's three projection masks."""
    saved = tuple(blocks)
    errors = legality_errors(saved)
    if errors:
        return CheckResult(False, False, (), (), (), "Invalid placement", errors)
    target_coords = target.coords if isinstance(target, Level) else tuple(target)
    target_errors = legality_errors(target_coords)
    if target_errors:
        raise ValueError("Invalid target: " + " ".join(target_errors))
    actual_front, actual_left, actual_top = projections(saved)
    wanted_front, wanted_left, wanted_top = projections(target_coords)
    front_diff = tuple((z, col) for z in range(MAX_HEIGHT) for col in range(WIDTH)
                       if actual_front[z][col] != wanted_front[z][col])
    left_diff = tuple((z, col) for z in range(MAX_HEIGHT) for col in range(DEPTH)
                      if actual_left[z][col] != wanted_left[z][col])
    top_diff = tuple((y, x) for y in range(DEPTH) for x in range(WIDTH)
                     if actual_top[y][x] != wanted_top[y][x])
    passed = not front_diff and not left_diff and not top_diff
    return CheckResult(passed, True, front_diff, left_diff, top_diff,
                       "Complete" if passed else "Check projections")


class Board:
    """Atomic column edits. IDs never migrate to a different surviving block.

    Undo restores snapshots including their block IDs. New IDs are monotonic
    even across undo/reset, preventing a stale selection from selecting an
    unrelated newly created block. Invalid operations leave history untouched.
    """

    def __init__(self) -> None:
        self._blocks: Tuple[Block, ...] = ()
        self._history: List[Tuple[Block, ...]] = []
        self._next_id = 1

    @classmethod
    def from_coords(cls, blocks: Iterable[Cell]) -> "Board":
        saved = tuple(blocks)
        errors = legality_errors(saved)
        if errors:
            raise ValueError("Illegal board: " + " ".join(errors))
        board = cls()
        coords = coordinates(saved)
        if saved and all(isinstance(b, Block) for b in saved):
            board._blocks = tuple(saved)
            board._next_id = max(b.id for b in board._blocks) + 1
        else:
            board._blocks = tuple(Block(i + 1, *coord) for i, coord in enumerate(coords))
            board._next_id = len(board._blocks) + 1
        return board

    @property
    def blocks(self) -> Tuple[Block, ...]:
        return self._blocks

    @property
    def coords(self) -> Tuple[Coord, ...]:
        return tuple(b.coord for b in self._blocks)

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    def __len__(self) -> int:
        return len(self._blocks)

    def height_at(self, x: int, y: int) -> int:
        return sum(b.x == x and b.y == y for b in self._blocks)

    def block_by_id(self, block_id: int) -> Optional[Block]:
        return next((b for b in self._blocks if b.id == block_id), None)

    def top_at(self, x: int, y: int) -> Optional[Block]:
        return max((b for b in self._blocks if b.x == x and b.y == y), key=lambda b: b.z, default=None)

    def is_top(self, block_id: int) -> bool:
        block = self.block_by_id(block_id)
        return block is not None and self.top_at(block.x, block.y).id == block_id

    @staticmethod
    def _valid_column(x: int, y: int) -> bool:
        return all(isinstance(v, Integral) and not isinstance(v, bool) for v in (x, y)) and 0 <= x < WIDTH and 0 <= y < DEPTH

    def _commit(self, candidate: Tuple[Block, ...]) -> bool:
        if legality_errors(candidate):
            return False
        self._history.append(self._blocks)
        # The public capacity is 24; cap reversible history to a predictable,
        # small allocation during long demonstrations.
        if len(self._history) > 100:
            del self._history[0]
        self._blocks = candidate
        return True

    def add(self, x: int, y: int) -> Optional[Block]:
        if not self._valid_column(x, y) or len(self) >= CAPACITY:
            return None
        z = self.height_at(x, y)
        if z >= MAX_HEIGHT:
            return None
        block = Block(self._next_id, int(x), int(y), z)
        if not self._commit(self._blocks + (block,)):
            return None
        self._next_id += 1
        return block

    def remove_top(self, x: int, y: int) -> Optional[Block]:
        if not self._valid_column(x, y):
            return None
        block = self.top_at(x, y)
        if block is None:
            return None
        return block if self._commit(tuple(b for b in self._blocks if b.id != block.id)) else None

    def move_top(self, block_id: int, x: int, y: int) -> bool:
        block = self.block_by_id(block_id)
        if block is None or not self.is_top(block_id) or not self._valid_column(x, y):
            return False
        if (block.x, block.y) == (x, y):
            return True
        z = self.height_at(x, y)
        if z >= MAX_HEIGHT:
            return False
        moved = Block(block.id, int(x), int(y), z)
        return self._commit(tuple(moved if b.id == block_id else b for b in self._blocks))

    def undo(self) -> bool:
        if not self._history:
            return False
        self._blocks = self._history.pop()
        return True

    def reset(self) -> bool:
        return bool(self._blocks) and self._commit(())

    def judge(self, target: Union[Level, Iterable[Cell]]) -> CheckResult:
        return judge(self._blocks, target)


def _columns(heights: Iterable[Tuple[int, int, int]]) -> Tuple[Coord, ...]:
    return tuple((x, y, z) for x, y, height in heights for z in range(height))


LEVELS = (
    Level("intro", "入门", _columns(((1, 1, 1), (2, 1, 2), (1, 2, 1), (2, 2, 1)))),
    Level("steps", "阶梯", _columns(((1, 1, 1), (2, 1, 2), (3, 1, 3), (1, 2, 1), (2, 2, 2), (3, 2, 2)))),
    Level("notch", "错层", _columns(((0, 1, 2), (1, 1, 1), (2, 1, 3), (3, 1, 1), (4, 1, 2),
                                     (0, 2, 2), (2, 2, 1), (4, 2, 2)))),
)
LEVEL_BY_ID = {level.id: level for level in LEVELS}
