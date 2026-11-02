"""Bounded stroke data and retained town operations; matching is submit-only.

New saves store each distinct source drawing once. Instances and library cards
share its immutable tuple rather than duplicating images or decoded sprites.
"""
from dataclasses import dataclass
import json
import math
from pathlib import Path
import uuid

import cv2
import numpy as np

from .model import (TownModel, MAX_OBJECTS, MAX_HISTORY,
                    SIZE_FACTORS, GRID_SIZES, _identifier, _position,
                    _variant, plane_for, snap_position)
from .rules import CATEGORIES, frozen_mask

SKETCH_WIDTH, SKETCH_HEIGHT = 640, 480
MAX_STROKES, MAX_POINTS = 64, 1024
MAX_LIBRARY = 8
# Up to 21 distinct 1024-point sources (editor + 8 cards + 12 objects),
# encoded once each. The old 256KiB grid-only limit cannot represent that data.
MAX_SAVE_BYTES = 2 * 1024 * 1024


def validated_strokes(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_STROKES:
        raise ValueError('drawing contains too many strokes')
    strokes, total = [], 0
    for line in value:
        if not isinstance(line, (list, tuple)) or not line:
            raise ValueError('each stroke must contain points')
        points = []
        for point in line:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError('a drawing point needs x and y')
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
                raise ValueError('drawing points must be finite numbers')
            x, y = map(float, point)
            if not (0 <= x <= SKETCH_WIDTH and 0 <= y <= SKETCH_HEIGHT):
                raise ValueError('drawing point is outside the blackboard')
            points.append((x, y))
        total += len(points)
        if total > MAX_POINTS:
            raise ValueError('drawing contains too many points')
        strokes.append(tuple(points))
    return tuple(strokes)


def legacy_strokes(mask):
    """Display the actual old grid outline, never substitute a sketch template."""
    image = np.flipud(np.asarray(mask, np.uint8)) * 255
    contours, _ = cv2.findContours(image, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    result = []
    for contour in contours:
        points = [(110. + float(p[0]) * 60., 60. + float(p[1]) * 60.)
                  for p in contour[:, 0]]
        if len(points) > 1:
            points.append(points[0])
        if points:
            result.append(tuple(points))
    return validated_strokes(result)


@dataclass(frozen=True)
class SketchBlueprint:
    id: str
    category: str
    variant: str
    mask: np.ndarray
    shape_variant: str
    strokes: tuple
    source_kind: str = 'sketch'


@dataclass
class SketchObject:
    id: str
    blueprint_id: str
    category: str
    x: float
    y: float
    size_index: int
    variant: str
    mask: np.ndarray
    shape_variant: str
    strokes: tuple
    source_kind: str = 'sketch'

    @property
    def scale(self):
        return SIZE_FACTORS[self.size_index]

    @property
    def plane(self):
        return plane_for(self.category)


class SketchTownModel(TownModel):
    def __init__(self, root=None, matcher=None):
        super().__init__(root)
        if matcher is None:
            from .sketch_rules import SketchMatcher
            matcher = SketchMatcher(root)
        self.matcher = matcher
        self.strokes = ()
        self.sketch_revision = 0
        self.match_result = None
        self._active_stroke = False
        self._source_kind = 'sketch'
        self._legacy_mask = None
        self._submitted_revision = None
        self._history = []

    @property
    def point_count(self):
        return sum(len(line) for line in self.strokes)

    @property
    def stroke_active(self):
        return self._active_stroke

    def _remember_sketch(self):
        self._history.append((self.strokes, self._source_kind, self._legacy_mask))
        if len(self._history) > MAX_HISTORY:
            del self._history[0]

    def _changed_sketch(self):
        self.sketch_revision += 1
        self.board_revision += 1
        self.match_result = None
        self.matched_category = self.matched_variant = None
        self._submitted_revision = None

    def begin_stroke(self, point):
        point = validated_strokes(((tuple(point),),))[0][0]
        self.end_stroke()
        if len(self.strokes) >= MAX_STROKES or self.point_count >= MAX_POINTS:
            return False
        self._remember_sketch()
        self.strokes += ((point,),)
        self._active_stroke = True
        self._source_kind, self._legacy_mask = 'sketch', None
        self._changed_sketch()
        return True

    def append_point(self, point):
        point = validated_strokes(((tuple(point),),))[0][0]
        if not self._active_stroke or self.point_count >= MAX_POINTS:
            return False
        if math.dist(point, self.strokes[-1][-1]) < .75:
            return False
        self.strokes = self.strokes[:-1] + (self.strokes[-1] + (point,),)
        self._changed_sketch()
        return True

    def end_stroke(self):
        active, self._active_stroke = self._active_stroke, False
        return active

    def replace_strokes(self, strokes, remember=True):
        strokes = validated_strokes(strokes)
        self.end_stroke()
        if remember:
            self._remember_sketch()
        self.strokes = strokes
        self._source_kind, self._legacy_mask = 'sketch', None
        self._changed_sketch()

    def undo(self):
        self.end_stroke()
        if not self._history:
            return False
        self.strokes, self._source_kind, self._legacy_mask = self._history.pop()
        self._changed_sketch()
        return True

    def clear(self):
        self.end_stroke()
        if not self.strokes:
            return False
        self._remember_sketch()
        self.strokes = ()
        self._source_kind, self._legacy_mask = 'sketch', None
        self.board.fill(False)
        self._changed_sketch()
        return True

    def toggle(self, x, y):
        raise ValueError('the sketch editor records strokes, not grid cells')

    def submit(self):
        self.end_stroke()
        self.submit_count += 1
        self.match_result = self.matcher.match(self.strokes)
        self.match_count += 1
        self.matched_category = self.match_result.category
        self.matched_variant = getattr(self.match_result, 'template_id', None)
        self._submitted_revision = self.sketch_revision if self.matched_category else None
        return self.matched_category

    def _source_mask(self, category):
        # Legacy mask is compatibility/provenance only; new recognition never
        # consults a 7x7 matrix. Original source strokes remain authoritative.
        return (self._legacy_mask if self._source_kind == 'legacy_grid'
                else frozen_mask(self.rules.mask_for(category)))

    def make_blueprint(self):
        if self.matched_category is None or self._submitted_revision != self.sketch_revision:
            raise ValueError('submit an accepted drawing before converting')
        # One appearance per category is sufficient for the retained material
        # bar. Every placed instance still receives its own actual source.
        for blueprint in self.library:
            if blueprint.category == self.matched_category and blueprint.variant == 'default':
                return blueprint
        if len(self.library) >= MAX_LIBRARY:
            raise ValueError('material library is full')
        blueprint = SketchBlueprint(uuid.uuid4().hex, self.matched_category, 'default',
            self._source_mask(self.matched_category), str(self.matched_variant or 'sketch'),
            self.strokes, self._source_kind)
        self.library.append(blueprint)
        self.library_revision += 1
        return blueprint

    def add_instance(self, blueprint_id, x=.5, y=.65, mask=None, strokes=None,
                     source_kind=None):
        if len(self.instances) >= MAX_OBJECTS:
            raise ValueError('scene already contains 12 objects')
        blueprint = self.get_blueprint(blueprint_id)
        source = blueprint.strokes if strokes is None else validated_strokes(strokes)
        kind = blueprint.source_kind if source_kind is None else source_kind
        if kind not in ('sketch', 'legacy_grid'):
            raise ValueError('unknown drawing source kind')
        source_mask = blueprint.mask if mask is None else frozen_mask(mask)
        x, y = snap_position(blueprint.category, x, y)
        instance = SketchObject(uuid.uuid4().hex, blueprint.id, blueprint.category,
            x, y, 1, blueprint.variant, source_mask, blueprint.shape_variant, source, kind)
        self.instances.append(instance)
        self.selected_id = instance.id
        self.scene_revision += 1
        return instance

    def copy_instance(self, identifier):
        source = self.get_instance(identifier)
        cols, rows = GRID_SIZES[source.plane]
        col, row = min(cols-1, int(source.x*cols)), min(rows-1, int(source.y*rows))
        col += 1
        if col == cols:
            col, row = 0, (row+1) % rows
        result = self.add_instance(source.blueprint_id, (col+.5)/cols, (row+.5)/rows,
            mask=source.mask, strokes=source.strokes, source_kind=source.source_kind)
        result.strokes = source.strokes
        result.size_index = source.size_index
        return result

    def _restore_source(self, source):
        self.end_stroke()
        self._remember_sketch()
        self.strokes = source.strokes
        self._source_kind = source.source_kind
        self._legacy_mask = source.mask if source.source_kind == 'legacy_grid' else None
        self.board = source.mask.copy()
        self._changed_sketch()

    def restore_blueprint(self, blueprint_id):
        self._restore_source(self.get_blueprint(blueprint_id))

    def restore_instance(self, identifier):
        self._restore_source(self.get_instance(identifier))

    def snapshot(self):
        # Deduplicate shared drawings in the JSON as well as in memory.
        sources, lookup = [], {}
        def source_id(strokes, kind, mask):
            key = strokes, kind, mask.tobytes() if kind == 'legacy_grid' else None
            if key not in lookup:
                identifier = 's' + str(len(sources))
                lookup[key] = identifier
                record = dict(id=identifier, kind=kind, strokes=strokes)
                if kind == 'legacy_grid':
                    record['legacy_mask'] = mask.astype(np.uint8).tolist()
                sources.append(record)
            return lookup[key]
        editor = source_id(self.strokes, self._source_kind, self._legacy_mask)
        library = [dict(id=b.id, category=b.category, variant=b.variant,
            shape_variant=b.shape_variant, source_id=source_id(b.strokes,b.source_kind,b.mask)) for b in self.library]
        instances = [dict(id=o.id, blueprint_id=o.blueprint_id, category=o.category,
            x=o.x, y=o.y, size_index=o.size_index, variant=o.variant,
            shape_variant=o.shape_variant, plane=o.plane,
            source_id=source_id(o.strokes,o.source_kind,o.mask)) for o in self.instances]
        return dict(version=3, canvas=[SKETCH_WIDTH,SKETCH_HEIGHT], sources=sources,
            editor_source_id=editor, reference=self.reference, selected_id=self.selected_id,
            library=library, instances=instances)

    def save(self, path):
        path = Path(path)
        document = self.snapshot()
        self._validated_sketch_document(document)
        encoded = json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(encoded) > MAX_SAVE_BYTES:
            raise ValueError('work exceeds the save size limit')
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            with temporary.open('xb') as target:
                target.write(encoded)
                target.flush()
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path

    def _validated_sketch_document(self, data):
        fields = {'version','canvas','sources','editor_source_id','reference','selected_id','library','instances'}
        if not isinstance(data, dict) or set(data) != fields or data['version'] != 3 or data['canvas'] != [640,480]:
            raise ValueError('invalid sketch work schema')
        if data['reference'] not in CATEGORIES:
            raise ValueError('unknown category')
        if not isinstance(data['sources'], list) or len(data['sources']) > 1+MAX_LIBRARY+MAX_OBJECTS:
            raise ValueError('too many drawing sources')
        sources = {}
        for raw in data['sources']:
            if not isinstance(raw, dict) or raw.get('kind') not in ('sketch','legacy_grid'):
                raise ValueError('invalid drawing source')
            kind = raw['kind']
            if set(raw) != ({'id','kind','strokes','legacy_mask'} if kind == 'legacy_grid' else {'id','kind','strokes'}):
                raise ValueError('invalid drawing source fields')
            identifier = _identifier(raw['id'])
            if identifier in sources:
                raise ValueError('duplicate drawing source')
            sources[identifier] = (validated_strokes(raw['strokes']), kind,
                frozen_mask(raw['legacy_mask']) if kind == 'legacy_grid' else None)
        editor_id = _identifier(data['editor_source_id'])
        if editor_id not in sources:
            raise ValueError('missing editor source')
        if not isinstance(data['library'], list) or len(data['library']) > MAX_LIBRARY:
            raise ValueError('invalid material library')
        if not isinstance(data['instances'], list) or len(data['instances']) > MAX_OBJECTS:
            raise ValueError('scene cannot contain more than 12 objects')
        library, instances, ids = [], [], set()
        for raw in data['library']:
            if not isinstance(raw, dict) or set(raw) != {'id','category','variant','shape_variant','source_id'}:
                raise ValueError('invalid material entry')
            identifier = _identifier(raw['id'])
            category = raw['category']
            if identifier in ids or category not in CATEGORIES:
                raise ValueError('invalid material identity')
            ids.add(identifier)
            variant = _variant(raw['variant'])
            shape_variant = raw['shape_variant']
            if not isinstance(shape_variant, str) or not 1 <= len(shape_variant) <= 64:
                raise ValueError('invalid source template name')
            if raw['source_id'] not in sources:
                raise ValueError('missing material source')
            strokes,kind,mask = sources[raw['source_id']]
            if kind == 'legacy_grid' and self.rules.match_variant(mask) != (category,shape_variant):
                raise ValueError('legacy material does not match its original grid')
            mask = mask if mask is not None else frozen_mask(self.rules.mask_for(category))
            library.append(SketchBlueprint(identifier,category,variant,mask,shape_variant,strokes,kind))
        by_id = {b.id:b for b in library}
        for raw in data['instances']:
            if not isinstance(raw, dict) or set(raw) != {'id','blueprint_id','category','x','y','size_index','variant','shape_variant','plane','source_id'}:
                raise ValueError('invalid scene object')
            identifier = _identifier(raw['id'])
            if identifier in ids or raw['blueprint_id'] not in by_id:
                raise ValueError('invalid scene object identity')
            ids.add(identifier)
            bp = by_id[raw['blueprint_id']]
            if (raw['category'],raw['variant'],raw['shape_variant'],raw['plane']) != (bp.category,bp.variant,bp.shape_variant,bp.plane if hasattr(bp,'plane') else plane_for(bp.category)):
                raise ValueError('object disagrees with its material')
            if type(raw['size_index']) is not int or not 0 <= raw['size_index'] <= 2 or raw['source_id'] not in sources:
                raise ValueError('invalid object size or source')
            strokes,kind,mask = sources[raw['source_id']]
            mask = mask if mask is not None else bp.mask
            x,y = _position(bp.category,raw['x'],raw['y'])
            instances.append(SketchObject(identifier,bp.id,bp.category,x,y,raw['size_index'],bp.variant,mask,bp.shape_variant,strokes,kind))
        selected = data['selected_id']
        if selected is not None and selected not in {o.id for o in instances}:
            raise ValueError('selection refers to a missing object')
        return sources[editor_id], data['reference'], library, instances, selected

    def load(self, path):
        path = Path(path)
        if path.stat().st_size > MAX_SAVE_BYTES:
            raise ValueError('work exceeds the save size limit')
        data = json.loads(path.read_bytes().decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite work data')))
        if isinstance(data, dict) and data.get('version') == 2:
            board,reference,old_library,old_instances,selected = TownModel._validated_document(self,data)
            library = [SketchBlueprint(b.id,b.category,b.variant,b.mask,b.shape_variant,legacy_strokes(b.mask),'legacy_grid') for b in old_library]
            by_id = {b.id:b for b in library}
            instances = [SketchObject(o.id,o.blueprint_id,o.category,o.x,o.y,o.size_index,o.variant,o.mask,o.shape_variant,
                by_id[o.blueprint_id].strokes if np.array_equal(o.mask,by_id[o.blueprint_id].mask) else legacy_strokes(o.mask),'legacy_grid') for o in old_instances]
            editor = legacy_strokes(board), 'legacy_grid', frozen_mask(board)
        else:
            editor,reference,library,instances,selected = self._validated_sketch_document(data)
        self.strokes,self._source_kind,self._legacy_mask = editor
        self.reference,self.library,self.instances,self.selected_id = reference,library,instances,selected
        self._active_stroke = False
        self._history.clear()
        self._changed_sketch()
        self.scene_revision += 1
        self.library_revision += 1
        return self
