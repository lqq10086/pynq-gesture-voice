"""Small deterministic town state; no camera or inference work belongs here."""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import List, Optional
import uuid

import numpy as np

from .rules import CATEGORIES, Rules, frozen_mask, validated_mask

MAX_OBJECTS = 12
MAX_HISTORY = 64
MAX_SAVE_BYTES = 256 * 1024
SIZE_FACTORS = (0.78, 1.0, 1.22)
GRID_SIZES = {'ground': (8, 6), 'background': (8, 4)}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class Blueprint:
    id: str
    category: str
    variant: str
    mask: np.ndarray
    shape_variant: str = 'A'


@dataclass
class SceneObject:
    id: str
    blueprint_id: str
    category: str
    x: float
    y: float
    size_index: int
    variant: str
    mask: np.ndarray
    shape_variant: str = 'A'

    @property
    def scale(self):
        return SIZE_FACTORS[self.size_index]

    @property
    def plane(self):
        return plane_for(self.category)


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid stable identifier")
    return value


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("position must be a finite number")
    return float(value)


def plane_for(category):
    if category not in CATEGORIES:
        raise ValueError('unknown category')
    return 'background' if category == 'cloud' else 'ground'


def snap_position(category, x, y):
    cols, rows = GRID_SIZES[plane_for(category)]
    x, y = _number(x), _number(y)
    col = min(cols-1, max(0, math.floor(x*cols)))
    row = min(rows-1, max(0, math.floor(y*rows)))
    return (col+.5)/cols, (row+.5)/rows


def _position(category, x, y):
    position = _number(x), _number(y)
    if category=='car':
        from .road import ROAD_CENTER, snap_car_position
        if abs(position[1]-ROAD_CENTER)<1e-8:
            snapped=snap_car_position(*position)
            if abs(position[0]-snapped[0])>1e-8:
                raise ValueError('saved car must be on a valid road column')
            return snapped
    snapped = snap_position(category, *position)
    if any(abs(a-b) > 1e-8 for a, b in zip(position, snapped)):
        raise ValueError('saved object must be on a valid plane cell center')
    return snapped


def _variant(value):
    if value != "default":
        raise ValueError("unknown appearance variant")
    return value


class TownModel:
    def __init__(self, root=None):
        self.rules = Rules(root)
        self.board = np.zeros((7, 7), dtype=np.bool_)
        self.reference = "tree"
        self.library: List[Blueprint] = []
        self.instances: List[SceneObject] = []
        self.selected_id: Optional[str] = None
        self.board_revision = 0
        self.scene_revision = 0
        self.library_revision = 0
        self.match_count = 0
        self.submit_count = 0
        self.matched_category: Optional[str] = None
        self.matched_variant: Optional[str] = None
        self._classified_mask = self.board.tobytes()
        self._submitted_mask = None
        self._history = []

    @property
    def last_match(self):
        return self.matched_category

    @property
    def live_category(self):
        return self.matched_category

    @property
    def live_variant(self):
        return self.matched_variant

    def _evaluate(self):
        template = self.rules.match_template(self.board)
        self.matched_category = template.key if template is not None else None
        self.matched_variant = template.shape_variant if template is not None else None
        self._classified_mask = self.board.tobytes()
        self.match_count += 1

    def _changed_board(self):
        self.board_revision += 1
        self._submitted_mask = None
        self._evaluate()

    def _remember(self):
        self._history.append(self.board.copy())
        if len(self._history) > MAX_HISTORY:
            del self._history[0]

    def toggle(self, x: int, y: int) -> bool:
        if type(x) is not int or type(y) is not int or not (0 <= x < 7 and 0 <= y < 7):
            raise ValueError("cell is outside the 7 by 7 board")
        self._remember()
        self.board[y, x] = not bool(self.board[y, x])
        self._changed_board()
        return bool(self.board[y, x])

    def undo(self) -> bool:
        if not self._history:
            return False
        self.board = self._history.pop()
        self._changed_board()
        return True

    def clear(self) -> bool:
        if not bool(self.board.any()):
            return False
        self._remember()
        self.board.fill(False)
        self._changed_board()
        return True

    def set_reference(self, category: str):
        if category not in CATEGORIES:
            raise ValueError("unknown reference category")
        self.reference = category

    def submit(self) -> Optional[str]:
        self.submit_count += 1
        # Compatibility for callers assigning a complete board directly. Normal
        # editor mutations already updated the cached result, so Submit is O(1).
        if self.board.tobytes() != self._classified_mask:
            self._changed_board()
        self._submitted_mask = self.board.copy() if self.matched_category is not None else None
        return self.matched_category

    def make_blueprint(self) -> Blueprint:
        if self.matched_category is None or self._submitted_mask is None or not np.array_equal(self.board, self._submitted_mask):
            raise ValueError("submit a matching board before converting")
        for blueprint in self.library:
            if blueprint.category == self.matched_category and blueprint.shape_variant == self.matched_variant and blueprint.variant == "default":
                return blueprint
        blueprint = Blueprint(uuid.uuid4().hex, self.matched_category, "default", frozen_mask(self.board), self.matched_variant)
        self.library.append(blueprint)
        self.library_revision += 1
        return blueprint

    def get_blueprint(self, identifier: str) -> Blueprint:
        for blueprint in self.library:
            if blueprint.id == identifier:
                return blueprint
        raise KeyError(identifier)

    def get_instance(self, identifier: str) -> SceneObject:
        for instance in self.instances:
            if instance.id == identifier:
                return instance
        raise KeyError(identifier)

    def add_instance(self, blueprint_id: str, x=.5, y=.65, mask=None) -> SceneObject:
        if len(self.instances) >= MAX_OBJECTS:
            raise ValueError("scene already contains 12 objects")
        blueprint = self.get_blueprint(blueprint_id)
        source_mask = blueprint.mask if mask is None else frozen_mask(mask)
        if self.rules.match_variant(source_mask) != (blueprint.category, blueprint.shape_variant):
            raise ValueError('instance source silhouette differs from its blueprint')
        if np.array_equal(source_mask, blueprint.mask):
            source_mask = blueprint.mask
        x, y = snap_position(blueprint.category, x, y)
        instance = SceneObject(uuid.uuid4().hex, blueprint.id, blueprint.category,
                               x, y, 1, blueprint.variant, source_mask, blueprint.shape_variant)
        self.instances.append(instance)
        self.selected_id = instance.id
        self.scene_revision += 1
        return instance

    def move(self, identifier: str, x: float, y: float) -> SceneObject:
        instance = self.get_instance(identifier)
        position = snap_position(instance.category, x, y)
        if position != (instance.x, instance.y):
            instance.x, instance.y = position
            self.scene_revision += 1
        return instance

    def copy_instance(self, identifier: str) -> SceneObject:
        source = self.get_instance(identifier)
        cols, rows = GRID_SIZES[source.plane]
        col, row = min(cols-1, int(source.x*cols)), min(rows-1, int(source.y*rows))
        col += 1
        if col == cols:
            col, row = 0, (row+1) % rows
        copy = self.add_instance(source.blueprint_id, (col+.5)/cols, (row+.5)/rows, mask=source.mask)
        copy.size_index = source.size_index
        return copy

    def delete_instance(self, identifier: str) -> bool:
        for index, instance in enumerate(self.instances):
            if instance.id == identifier:
                del self.instances[index]
                if self.selected_id == identifier:
                    self.selected_id = None
                self.scene_revision += 1
                return True
        return False

    def restore_blueprint(self, blueprint_id: str):
        """Return to its exact editable source; library and placed instances survive."""
        blueprint = self.get_blueprint(blueprint_id)
        if not np.array_equal(self.board, blueprint.mask):
            self._remember()
            self.board = blueprint.mask.copy()
            self._changed_board()

    def restore_instance(self, identifier: str):
        """Keep each placed object's actual source translation, not a library default."""
        source = self.get_instance(identifier)
        if not np.array_equal(self.board, source.mask):
            self._remember()
            self.board = source.mask.copy()
            self._changed_board()

    def snapshot(self):
        return {
            "version": 2,
            "board": self.board.astype(np.uint8).tolist(),
            "reference": self.reference,
            "selected_id": self.selected_id,
            "library": [{"id": bp.id, "category": bp.category, "variant": bp.variant, "shape_variant": bp.shape_variant,
                         "mask": bp.mask.astype(np.uint8).tolist()} for bp in self.library],
            "instances": [{"id": item.id, "blueprint_id": item.blueprint_id,
                           "category": item.category, "x": item.x, "y": item.y,
                           "size_index": item.size_index, "variant": item.variant, "shape_variant": item.shape_variant,
                           "plane": item.plane,
                           "mask": item.mask.astype(np.uint8).tolist()} for item in self.instances],
        }

    def save(self, path):
        path = Path(path)
        document = self.snapshot()
        # Validate even in-memory public fields before creating a supposedly valid save.
        self._validated_document(document)
        encoded = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_SAVE_BYTES:
            raise ValueError("work exceeds the save size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as target:
                target.write(encoded)
                target.flush()
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def _validated_document(self, document):
        if not isinstance(document, dict) or set(document) != {"version", "board", "reference", "selected_id", "library", "instances"}:
            raise ValueError("invalid work schema")
        if type(document["version"]) is not int or document["version"] != 2:
            raise ValueError("unsupported work version")
        board = validated_mask(document["board"])
        reference = document["reference"]
        if reference not in CATEGORIES:
            raise ValueError("unknown reference category")
        source_library, source_instances = document["library"], document["instances"]
        if not isinstance(source_library, list) or len(source_library) > 8:
            raise ValueError("invalid blueprint library")
        if not isinstance(source_instances, list) or len(source_instances) > MAX_OBJECTS:
            raise ValueError("scene cannot contain more than 12 objects")
        library, instances, ids, fingerprints = [], [], set(), set()
        for source in source_library:
            if not isinstance(source, dict) or set(source) != {"id", "category", "variant", "shape_variant", "mask"}:
                raise ValueError("invalid blueprint schema")
            identifier = _identifier(source["id"])
            mask = frozen_mask(source["mask"])
            category = source["category"]
            shape_variant = source['shape_variant']
            if category not in CATEGORIES or self.rules.match_variant(mask) != (category, shape_variant):
                raise ValueError("blueprint category does not match its full original occupancy")
            variant = _variant(source["variant"])
            fingerprint = (category, shape_variant, variant)
            if identifier in ids or fingerprint in fingerprints:
                raise ValueError("duplicate blueprint identifier or source")
            ids.add(identifier)
            fingerprints.add(fingerprint)
            library.append(Blueprint(identifier, category, variant, mask, shape_variant))
        by_id = {blueprint.id: blueprint for blueprint in library}
        for source in source_instances:
            fields = {"id", "blueprint_id", "category", "x", "y", "size_index", "variant", "shape_variant", "plane", "mask"}
            if not isinstance(source, dict) or set(source) != fields:
                raise ValueError("invalid instance schema")
            identifier = _identifier(source["id"])
            if identifier in ids:
                raise ValueError("duplicate stable identifier")
            ids.add(identifier)
            blueprint_id = _identifier(source["blueprint_id"])
            if blueprint_id not in by_id:
                raise ValueError("instance refers to a missing blueprint")
            blueprint = by_id[blueprint_id]
            mask = frozen_mask(source["mask"])
            if source["category"] != blueprint.category or source["variant"] != blueprint.variant or source['shape_variant'] != blueprint.shape_variant or source['plane'] != plane_for(blueprint.category) or self.rules.match_variant(mask) != (blueprint.category, blueprint.shape_variant):
                raise ValueError("instance original data does not agree with its blueprint")
            if np.array_equal(mask, blueprint.mask):
                mask = blueprint.mask
            size = source["size_index"]
            if type(size) is not int or not 0 <= size <= 2:
                raise ValueError("invalid size index")
            x, y = _position(blueprint.category, source['x'], source['y'])
            instances.append(SceneObject(identifier, blueprint_id, blueprint.category, x, y,
                                          size, blueprint.variant, mask, blueprint.shape_variant))
        selected = document["selected_id"]
        if selected is not None:
            _identifier(selected)
        if selected is not None and selected not in {item.id for item in instances}:
            raise ValueError("selection refers to a missing instance")
        return board, reference, library, instances, selected

    def load(self, path):
        path = Path(path)
        if path.stat().st_size > MAX_SAVE_BYTES:
            raise ValueError("work file exceeds the size limit")
        with path.open("rb") as source:
            data = source.read(MAX_SAVE_BYTES + 1)
        if len(data) > MAX_SAVE_BYTES:
            raise ValueError("work file exceeds the size limit")
        try:
            document = json.loads(data.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON value")))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("invalid work file") from exc
        board, reference, library, instances, selected = self._validated_document(document)
        # All validation has completed before the first state mutation.
        self.board, self.reference = board, reference
        self.library, self.instances, self.selected_id = library, instances, selected
        self._history.clear()
        self._changed_board()
        self.scene_revision += 1
        self.library_revision += 1
        return self
