"""Bounded four-class stroke matching; no camera analysis or learned model.

Templates and query strokes share the same 64x64 rasterization. Distance maps
for at most twenty templates are cached once. A submission computes one query
distance map, two directional line distances, and a few coarse shape features.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import time

import cv2
import numpy as np

CANVAS_WIDTH, CANVAS_HEIGHT = 640, 480
MAX_STROKES, MAX_POINTS = 64, 1024
RASTER_SIZE, PADDING = 64, 6
CATEGORIES = ('tree', 'house', 'cloud', 'car')


@dataclass(frozen=True)
class MatchResult:
    category: object
    score: float
    margin: float
    reason: str
    template_id: object
    distances: dict

    @property
    def accepted(self):
        return self.category is not None


@dataclass(frozen=True)
class SketchTemplate:
    id: str
    category: str
    strokes: tuple


def validated_strokes(strokes):
    """Return finite immutable logical-pixel strokes, preserving pen lifts."""
    if not isinstance(strokes, (list, tuple)) or len(strokes) > MAX_STROKES:
        raise ValueError('A sketch may contain at most 64 strokes')
    result, count = [], 0
    for stroke in strokes:
        if not isinstance(stroke, (list, tuple, np.ndarray)):
            raise ValueError('Each stroke must be a sequence of xy points')
        points = np.asarray(stroke, dtype=np.float64)
        if points.size == 0:
            continue
        if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
            raise ValueError('Stroke points must be finite xy pairs')
        count += len(points)
        if count > MAX_POINTS:
            raise ValueError('A sketch may contain at most 1024 points')
        if ((points < 0).any() or (points[:, 0] > CANVAS_WIDTH).any()
                or (points[:, 1] > CANVAS_HEIGHT).any()):
            raise ValueError('Stroke points exceed the 640x480 logical board')
        result.append(tuple((float(p[0]), float(p[1])) for p in points))
    return tuple(result)


def normalized_ink(strokes):
    """Equal scaling only: keep aspect and upright direction; never join lifts."""
    strokes = validated_strokes(strokes)
    if not strokes:
        return None, 'empty'
    arrays = [np.asarray(stroke, np.float32) for stroke in strokes]
    points = np.concatenate(arrays)
    lower, upper = points.min(axis=0), points.max(axis=0)
    span = upper-lower
    length = sum(float(np.linalg.norm(np.diff(a, axis=0), axis=1).sum())
                 for a in arrays if len(a) > 1)
    if min(span) < 6 or max(span) < 18 or length < 28:
        return None, 'too_small_or_short'
    if not .20 <= float(span[0]/span[1]) <= 5.0:
        return None, 'line_or_extreme_aspect'
    scale = (RASTER_SIZE-2*PADDING-1)/float(span.max())
    offset = (RASTER_SIZE-1-span*scale)*.5
    ink = np.zeros((RASTER_SIZE, RASTER_SIZE), np.uint8)
    for array in arrays:
        local = np.rint((array-lower)*scale+offset).astype(np.int32)
        if len(local) > 1:
            cv2.polylines(ink, [local], False, 255, 2, cv2.LINE_8)
        else:
            cv2.circle(ink, tuple(local[0]), 1, 255, -1)
    if np.count_nonzero(ink) < 35:
        return None, 'too_little_ink'
    return ink, None


def _features(ink):
    ys, xs = np.nonzero(ink)
    crop = ink[ys.min():ys.max()+1, xs.min():xs.max()+1] > 0
    h, w = crop.shape
    xpos = np.arange(w)[None, :]
    left = np.where(crop, xpos, w).min(axis=1)
    right = np.where(crop, xpos, -1).max(axis=1)
    widths = np.maximum(0, right-left+1)
    low = widths[int(h*.76):]
    narrow = float(np.median(low[low > 0])/w) if np.any(low > 0) else 0.
    # Use the lowest 15%, below the car body's baseline. At 64px a wide car
    # is only ~24px high, so including the lower quarter merges both wheels.
    lower_columns = np.any(crop[int(h*.85):], axis=0)
    groups = int(np.count_nonzero(np.diff(np.r_[False, lower_columns, False].astype(np.int8)) == 1))
    top = np.where(crop, np.arange(h)[:, None], h).min(axis=0)/float(h)
    def region(a, b):
        return top[max(0, int(w*a)):max(1, int(w*b))]
    l, c, r = region(.20,.44), region(.46,.54), region(.56,.80)
    lobes = float(c.mean()-.5*(l.min()+r.min()))
    body_rows = widths[int(h*.55):max(int(h*.55)+1,int(h*.90))]
    body_variation = float((np.percentile(body_rows,90)-np.percentile(body_rows,10))/w)
    tail_rows = widths[int(h*.75):max(int(h*.75)+1,int(h*.95))]
    tail_variation = float((np.percentile(tail_rows,90)-np.percentile(tail_rows,10))/w)
    # Class identity comes from structure, not which fixed outline is nearest.
    # A fir needs two widening crowns separated by an inward shoulder. A house
    # has one roof and a near-constant-width wall below its *actual* eaves.
    profile=widths.astype(np.float64)/w
    smooth=np.median(np.lib.stride_tricks.sliding_window_view(
        np.pad(profile,(1,1),mode='edge'),3),axis=1)
    notch=0.
    for row in range(max(3,int(h*.18)),max(4,int(h*.56))):
        before=profile[max(0,row-max(3,int(h*.14))):row-1]
        after=smooth[row+max(2,int(h*.10)):max(row+3,int(h*.81))]
        if before.size and after.size:
            first=float(before.max());second=float(after.max())
            if second>=first+.10:
                notch=max(notch,min(first-smooth[row],second-smooth[row]))
    roof_end=int(np.argmax(smooth[:max(3,h-3)]))
    wall_start=roof_end+max(3,int(h*.075))
    house_fit=False;roof_error=wall_error=center_error=1.
    if .16*h<=roof_end<=.80*h and h-wall_start>=max(5,int(h*.12)):
        start=max(1,int(h*.05));stop=max(start+3,roof_end)
        rows=np.arange(start,stop,dtype=np.float64)
        values=smooth[start:stop]
        slope=float(np.dot(rows-rows.mean(),values-values.mean())/
                    max(1.,np.dot(rows-rows.mean(),rows-rows.mean())))
        predicted=values.mean()+slope*(rows-rows.mean())
        roof_error=float(np.percentile(np.abs(values-predicted),85))
        walls=smooth[wall_start:max(wall_start+1,h-2)]
        wall_width=float(np.median(walls))
        wall_error=float(np.percentile(np.abs(walls-wall_width),90))
        wall_rows=np.arange(len(walls),dtype=np.float64)
        wall_trend=abs(float(np.dot(wall_rows-wall_rows.mean(),walls-walls.mean())/
                             max(1.,np.dot(wall_rows-wall_rows.mean(),wall_rows-wall_rows.mean()))))*(len(walls)-1)
        centers=(left+right)/(2.*w)
        center_error=float(np.percentile(np.abs(centers[wall_start:h-2]-.5),90))
        house_fit=(profile[0]<=.24 and slope>.008 and roof_error<=.062
                   and wall_error<=.14 and wall_trend<=.13 and center_error<=.14
                   and .26<=wall_width<=1.01 and notch<.045)
    return dict(aspect=float(w/h), lower_width=narrow, bottom_groups=groups,
                upper_lobes=lobes, body_width_variation=body_variation,
                tail_width_variation=tail_variation,crown_notch=float(notch),
                house_profile_fit=bool(house_fit),roof_error=roof_error,
                wall_error=wall_error,wall_center_error=center_error)


def _structure_penalty(category, f):
    # Broad preferences only; no exact polygon/circle fitting or closure test.
    if category == 'tree':
        return ((.20 if f['crown_notch']<.045 else 0.)
                + max(0., f['lower_width']-.50)*.10 + max(0., f['aspect']-1.15)*.04
                + max(0., f['tail_width_variation']-.10)*.22)
    if category == 'house':
        return 0. if f['house_profile_fit'] else .20
    if category == 'cloud':
        return (.05+max(0.,-f['upper_lobes'])*.08) if f['upper_lobes'] < .025 else 0.
    if category == 'car':
        return (.060 if f['bottom_groups'] < 2 else 0.) + max(0.,1.25-f['aspect'])*.04
    return 0.


class SketchMatcher:
    def __init__(self, root=None):
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        path = root if root.suffix.lower() == '.json' else root/'assets/sketch/templates.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        entries = document['templates']
        if document.get('version') != 1 or not 4 <= len(entries) <= 20:
            raise ValueError('Unsupported sketch template bank')
        self.maximum_score = float(document.get('maximum_score', .055))
        self.minimum_margin = float(document.get('minimum_margin', .008))
        self.templates, self._prepared = [], []
        ids, rasters, categories = set(), set(), set()
        for entry in entries:
            category, identifier = entry['category'], entry['id']
            if category not in CATEGORIES or identifier in ids:
                raise ValueError('Unknown category or duplicate template id')
            strokes = validated_strokes(entry['strokes'])
            ink, error = normalized_ink(strokes)
            if error:
                raise ValueError('Invalid reference drawing: '+str(error))
            fingerprint = ink.tobytes()
            if fingerprint in rasters:
                raise ValueError('Duplicate normalized sketch template')
            ids.add(identifier); rasters.add(fingerprint); categories.add(category)
            template = SketchTemplate(identifier, category, strokes)
            self.templates.append(template)
            distance = cv2.distanceTransform(255-ink, cv2.DIST_L2, 3)
            self._prepared.append((template, ink > 0, distance, _features(ink)))
        if categories != set(CATEGORIES):
            raise ValueError('All four sketch categories are required')
        self.templates = tuple(self.templates)
        self.match_calls = 0
        self._last_ms = 0.
        self._maximum_ms = 0.

    def match(self, strokes):
        started = time.perf_counter()
        self.match_calls += 1
        try:
            ink, error = normalized_ink(strokes)
            if error:
                return MatchResult(None, float('inf'), 0., error, None, {})
            active = ink > 0
            query_distance = cv2.distanceTransform(255-ink, cv2.DIST_L2, 3)
            features = _features(ink)
            best = {category: (float('inf'), None) for category in CATEGORIES}
            for template, reference, distance, ref_features in self._prepared:
                score = .5*(float(distance[active].mean())
                             + float(query_distance[reference].mean()))/(RASTER_SIZE-2*PADDING)
                score += .012*abs(np.log(features['aspect']/ref_features['aspect']))
                score += .012*abs(features['lower_width']-ref_features['lower_width'])
                score += _structure_penalty(template.category, features)
                if score < best[template.category][0]:
                    best[template.category] = (float(score), template.id)
            if features['house_profile_fit']:
                # Fit one triangular roof and one rectangular body as a family,
                # so roof height/overhang need not equal a single stored ratio.
                # The strict structure gate above prevents a one-roof house
                # from becoming a tree merely because its walls are narrow.
                family_score=(.20*features['roof_error']+.12*features['wall_error']
                              +.04*features['wall_center_error'])
                if family_score<best['house'][0]:
                    best['house']=(float(family_score),'house_roof_and_walls')
            ranked = sorted(best, key=lambda category: best[category][0])
            category = ranked[0]
            score, identifier = best[category]
            margin = best[ranked[1]][0]-score
            distances = {k: v[0] for k,v in best.items()}
            if score > self.maximum_score:
                return MatchResult(None, score, margin, 'shape_too_different', identifier, distances)
            if margin < self.minimum_margin:
                return MatchResult(None, score, margin, 'ambiguous', identifier, distances)
            return MatchResult(category, score, margin, 'matched', identifier, distances)
        finally:
            self._last_ms = (time.perf_counter()-started)*1000.
            self._maximum_ms = max(self._maximum_ms, self._last_ms)

    def metrics(self):
        return dict(match_calls=self.match_calls, template_count=len(self.templates),
                    raster_size=RASTER_SIZE, last_match_ms=self._last_ms,
                    maximum_match_ms=self._maximum_ms,
                    cached_distance_bytes=sum(a[2].nbytes for a in self._prepared),
                    maximum_score=self.maximum_score, minimum_margin=self.minimum_margin,
                    learned_model=False, score_is_probability=False,
                    recognition_scope='submitted pen coordinates only; no camera pixels')
