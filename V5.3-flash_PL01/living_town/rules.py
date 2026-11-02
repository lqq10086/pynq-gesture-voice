"""Eight explicit silhouettes; translate only, using the whole 7 x 7 state.

Bounding-box normalization removes empty margins only. Every occupied cell,
including any isolated extra cell, participates. There is no nearest match,
resizing, rotation, component selection or reference-driven classification.
"""
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np

WIDTH = HEIGHT = 7
CATEGORIES = ("tree", "car", "house", "cloud")


def validated_mask(value) -> np.ndarray:
    try:
        array = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("occupancy must be a 7 by 7 binary matrix") from exc
    if array.shape != (HEIGHT, WIDTH) or array.dtype.kind not in "biuf":
        raise ValueError("occupancy must be a 7 by 7 binary matrix")
    if not bool(np.all((array == 0) | (array == 1))):
        raise ValueError("occupancy contains a value other than 0 or 1")
    return np.array(array, dtype=np.bool_, copy=True)


def frozen_mask(value) -> np.ndarray:
    result = validated_mask(value)
    result.setflags(write=False)
    return result


def normalized_key(mask):
    """A key of ALL occupied cells, or None for an empty board."""
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    cropped = np.ascontiguousarray(mask[ys.min():ys.max()+1, xs.min():xs.max()+1], dtype=np.bool_)
    return cropped.shape[0], cropped.shape[1], cropped.tobytes()


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    rows: Tuple[str, ...]
    mask: np.ndarray
    count: int
    shape_variant: str

    @property
    def variant(self):
        return self.shape_variant


class Rules:
    def __init__(self, root=None):
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        self.path = root if root.suffix.lower() == ".json" else root / "assets" / "town" / "templates.json"
        with self.path.open("r", encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, dict) or any(type(document.get(k)) is not int for k in ("version", "width", "height")):
            raise ValueError("invalid template document")
        if document["version"] != 2 or document["width"] != WIDTH or document["height"] != HEIGHT or document.get("matching") != "full-occupancy-bounding-box-translation-only":
            raise ValueError("unsupported template dimensions, matching policy or version")
        entries = document.get("templates")
        if not isinstance(entries, list) or len(entries) != 8:
            raise ValueError("exactly eight explicit silhouettes are required")
        variants: Dict[str, list] = {category: [] for category in CATEGORIES}
        lookup = {}
        identities = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid template entry")
            category, shape_variant = entry.get("key"), entry.get("shape_variant")
            if category not in CATEGORIES or shape_variant not in ("A", "B") or (category, shape_variant) in identities:
                raise ValueError("missing, duplicate or unknown silhouette identity")
            title, rows, count = entry.get("title"), entry.get("rows"), entry.get("count")
            if not isinstance(title, str) or not title or len(title) > 32:
                raise ValueError("invalid template title")
            if not isinstance(rows, list) or not 1 <= len(rows) <= HEIGHT or any(not isinstance(row, str) for row in rows):
                raise ValueError("invalid silhouette rows")
            width = len(rows[0])
            if not 1 <= width <= WIDTH or any(len(row) != width or set(row) - {".", "#"} for row in rows):
                raise ValueError("silhouette rows must be equal width, using dots and hashes")
            cropped = np.array([[cell == "#" for cell in row] for row in reversed(rows)], dtype=np.bool_)
            if not cropped[0].any() or not cropped[-1].any() or not cropped[:, 0].any() or not cropped[:, -1].any():
                raise ValueError("silhouette source must have a tight, nonempty bounding box")
            actual_count = int(cropped.sum())
            if type(count) is not int or count != actual_count:
                raise ValueError("template block count is incorrect")
            mask = np.zeros((HEIGHT, WIDTH), dtype=np.bool_)
            x0 = (WIDTH-width)//2
            mask[:len(rows), x0:x0+width] = cropped
            mask.setflags(write=False)
            key = normalized_key(mask)
            if key in lookup:
                raise ValueError("silhouettes conflict after translation normalization")
            template = Template(category, title, tuple(rows), mask, count, shape_variant)
            lookup[key] = template
            identities.add((category, shape_variant))
            variants[category].append(template)
        if any({t.shape_variant for t in variants[category]} != {"A", "B"} for category in CATEGORIES):
            raise ValueError("each category must have two explicit silhouettes")
        self.variants = {category: tuple(sorted(items, key=lambda t: t.shape_variant)) for category, items in variants.items()}
        self.templates = {category: self.variants[category][0] for category in CATEGORIES}
        self._lookup = lookup

    def match_template(self, mask) -> Optional[Template]:
        return self._lookup.get(normalized_key(validated_mask(mask)))

    def match(self, mask) -> Optional[str]:
        template = self.match_template(mask)
        return template.key if template is not None else None

    def match_variant(self, mask):
        template = self.match_template(mask)
        return (template.key, template.shape_variant) if template is not None else None

    def mask_for(self, key: str, shape_variant=None) -> np.ndarray:
        if shape_variant is None:
            return self.templates[key].mask.copy()
        for template in self.variants[key]:
            if template.shape_variant == shape_variant:
                return template.mask.copy()
        raise KeyError((key, shape_variant))
