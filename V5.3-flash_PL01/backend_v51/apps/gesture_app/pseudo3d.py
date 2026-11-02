"""Small cached CPU sprites for the playground's toy bricks and ball.

There is no 3D runtime. Lighting, bevels, studs and soft shadows are calculated
only on a sprite cache miss. Subsequent draws blend the clipped sprite rectangle
into a BGR uint8 frame; they never allocate an image-sized overlay or blur a frame.

Coordinates describe geometry, not the drop shadow. A block's top diamond is
(cx, cy-depth), (cx+width, cy), (cx, cy+depth), (cx-width, cy); its sides extend
down by ``height``. Returned bounds are the clipped rectangle actually painted.
Drawing does not change either game's collision geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from pathlib import Path
from typing import Dict, Sequence, Tuple
import weakref

import cv2
import numpy as np


Bounds = Tuple[int, int, int, int]
Color = Tuple[int, int, int]
# Four palette, four floor, four stacked and four selected brick variants fit
# together, so a mixed-color tower does not regenerate sprites during a drag.
BLOCK_CACHE_SIZE = 16
SPHERE_CACHE_SIZE = 8
MAX_HALF_WIDTH = 120
MAX_HALF_DEPTH = 72
MAX_BLOCK_HEIGHT = 160
MAX_SPHERE_RADIUS = 96
_LIVE_SPRITES = weakref.WeakValueDictionary()
_BAKED_BLOCKS = {}
_BAKED_SPHERES = {}
_ASSET_RECORD = {"loaded": False, "source": None, "preloaded_entries": 0,
                 "preloaded_blocks": 0, "preloaded_spheres": 0,
                 "pixel_buffer_bytes": 0}
_ASSET_FORMAT = "pynq-pseudo3d-v1"


@dataclass(frozen=True)
class _Sprite:
    rgb: np.ndarray
    alpha: np.ndarray
    premultiplied: np.ndarray
    inverse: np.ndarray
    anchor: Tuple[int, int]
    kind: str

    @property
    def nbytes(self) -> int:
        return int(self.rgb.nbytes + self.alpha.nbytes
                   + self.premultiplied.nbytes + self.inverse.nbytes)


def _color(color: Sequence[int]) -> Color:
    if len(color) != 3:
        raise ValueError("A sprite color must contain three BGR components")
    result = []
    for value in color:
        value = float(value)
        if not math.isfinite(value):
            raise ValueError("Color components must be finite")
        # Four-level quantisation avoids new sprites for tiny tint changes.
        result.append(min(255, max(0, int(round(value / 4.0)) * 4)))
    return tuple(result)  # type: ignore[return-value]


def _dimension(value: float, maximum: int, minimum: int = 3) -> int:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Sprite dimensions must be finite and positive")
    return min(maximum, max(minimum, int(round(value))))


def _point(point: Sequence[float]) -> Tuple[int, int]:
    if len(point) != 2 or not all(math.isfinite(float(v)) for v in point):
        raise ValueError("A sprite position must contain two finite coordinates")
    return int(round(float(point[0]))), int(round(float(point[1])))


def _canvas(shape: Tuple[int, int]) -> Tuple[np.ndarray, np.ndarray]:
    return np.zeros(shape + (3,), np.float32), np.zeros(shape, np.float32)


def _over(canvas: np.ndarray, alpha: np.ndarray, color: np.ndarray,
          mask: np.ndarray, opacity: float = 1.0) -> None:
    """Composite one material into a cache-miss-only premultiplied canvas."""
    coverage = mask.astype(np.float32) * (opacity / 255.0)
    canvas *= (1.0 - coverage)[:, :, None]
    canvas += np.asarray(color, dtype=np.float32) * coverage[:, :, None]
    alpha *= 1.0 - coverage
    alpha += coverage


def _finish(canvas: np.ndarray, alpha: np.ndarray,
            anchor: Tuple[int, int], kind: str) -> _Sprite:
    rgb = np.clip(canvas / np.maximum(alpha[:, :, None], 1e-7), 0, 255)
    rgb = np.rint(rgb).astype(np.uint8)
    mask = np.rint(np.clip(alpha, 0, 1) * 255).astype(np.uint8)
    return _from_pixels(rgb, mask, anchor, kind)


def _from_pixels(rgb: np.ndarray, mask: np.ndarray,
                 anchor: Tuple[int, int], kind: str) -> _Sprite:
    """Reconstruct blending factors using only integer math for baked assets."""
    weight = (mask.astype(np.uint16) * 256 + 127) // 255
    premultiplied = rgb.astype(np.uint16) * weight[:, :, None]
    inverse = 256 - weight
    for array in (rgb, mask, premultiplied, inverse):
        array.setflags(write=False)
    sprite = _Sprite(rgb, mask, premultiplied, inverse, anchor, kind)
    _LIVE_SPRITES[id(sprite)] = sprite
    return sprite


def _rounded_polygon(points: Sequence[Tuple[int, int]], radius: float) -> np.ndarray:
    """A few quadratic corner samples give plastic edges a small bevel."""
    vertices = np.asarray(points, np.float32)
    contour = []
    for index, vertex in enumerate(vertices):
        before = vertices[index - 1] - vertex
        after = vertices[(index + 1) % len(vertices)] - vertex
        start = vertex + before * min(0.22, radius / max(1.0, float(np.linalg.norm(before))))
        end = vertex + after * min(0.22, radius / max(1.0, float(np.linalg.norm(after))))
        for t in (0.0, 0.25, 0.5, 0.75, 1.0):
            contour.append((1 - t) ** 2 * start + 2 * (1 - t) * t * vertex + t ** 2 * end)
    return np.rint(contour).astype(np.int32)


def _polygon(shape: Tuple[int, int], points: Sequence[Tuple[int, int]],
             radius: float = 2.0) -> np.ndarray:
    mask = np.zeros(shape, np.uint8)
    cv2.fillConvexPoly(mask, _rounded_polygon(points, radius), 255, cv2.LINE_AA)
    return mask


def _stroke(canvas: np.ndarray, alpha: np.ndarray, points: Sequence[Tuple[int, int]],
            color: np.ndarray, opacity: float, thickness: int = 1,
            closed: bool = False) -> None:
    mask = np.zeros(alpha.shape, np.uint8)
    cv2.polylines(mask, [np.asarray(points, np.int32)], closed, 255,
                  thickness, cv2.LINE_AA)
    _over(canvas, alpha, color, mask, opacity)


def _ellipse(shape: Tuple[int, int], center: Tuple[int, int],
             axes: Tuple[int, int]) -> np.ndarray:
    mask = np.zeros(shape, np.uint8)
    cv2.ellipse(mask, center, axes, 0, 0, 360, 255, -1, cv2.LINE_AA)
    return mask


@lru_cache(maxsize=BLOCK_CACHE_SIZE)
def _block_sprite(half_width: int, half_depth: int, height: int,
                  color: Color, selected: bool, studs: bool, shadow: bool) -> _Sprite:
    baked = _BAKED_BLOCKS.get((half_width, half_depth, height, color,
                                selected, studs, shadow))
    if baked is not None:
        return baked
    padding = max(7, int(round(half_width * 0.12)))
    shadow_dx = max(2, int(round(half_width * 0.07)))
    shadow_dy = max(3, int(round(half_depth * 0.15)))
    shape = (2 * half_depth + height + 2 * padding + shadow_dy,
             2 * half_width + 2 * padding + shadow_dx)
    cx, cy = padding + half_width, padding + half_depth
    north = (cx, cy - half_depth)
    east = (cx + half_width, cy)
    south = (cx, cy + half_depth)
    west = (cx - half_width, cy)
    south_bottom = (south[0], south[1] + height)
    east_bottom = (east[0], east[1] + height)
    west_bottom = (west[0], west[1] + height)
    canvas, alpha = _canvas(shape)
    base = np.asarray(color, np.float32)
    yy, xx = np.mgrid[:shape[0], :shape[1]].astype(np.float32)

    silhouette = _polygon(shape, [north, east, east_bottom, south_bottom,
                                   west_bottom, west], 2.5)
    if shadow:
        offset = np.float32([[1, 0, shadow_dx], [0, 1, shadow_dy]])
        shade = cv2.warpAffine(silhouette, offset, (shape[1], shape[0]))
        shade = cv2.GaussianBlur(shade, (0, 0), max(1.1, half_width * 0.025))
        _over(canvas, alpha, np.asarray((17, 22, 29), np.float32), shade, 0.28)
    if selected:
        outline = cv2.dilate(silhouette, np.ones((5, 5), np.uint8))
        glow = cv2.GaussianBlur(outline, (0, 0), 2.0)
        _over(canvas, alpha, np.asarray((236, 229, 151), np.float32), glow, 0.28)

    left_mask = _polygon(shape, [west, south, south_bottom, west_bottom])
    right_mask = _polygon(shape, [south, east, east_bottom, south_bottom])
    top_mask = _polygon(shape, [north, east, south, west], 2.7)
    vertical = np.clip((yy - cy) / max(1.0, float(height + half_depth)), 0, 1)
    lateral = (xx - cx) / float(half_width)
    left_light = (0.91 - 0.14 * vertical - 0.025 * lateral)[:, :, None]
    right_light = (0.73 - 0.13 * vertical - 0.025 * lateral)[:, :, None]
    top_light = np.clip(0.20 - 0.065 * lateral - 0.035 * (yy - cy) / half_depth,
                        0.08, 0.31)[:, :, None]
    _over(canvas, alpha, base * left_light + 7.0, left_mask)
    _over(canvas, alpha, base * right_light + 3.0, right_mask)
    _over(canvas, alpha, base + (255 - base) * top_light, top_mask)

    # The upper left edge catches the key light; the center seam is subdued.
    _stroke(canvas, alpha, [west, north, east], base + (255 - base) * 0.64, 0.58)
    _stroke(canvas, alpha, [west, south, east], base + (255 - base) * 0.35, 0.62)
    _stroke(canvas, alpha, [south, south_bottom], base * 0.47, 0.24)
    _stroke(canvas, alpha, [west_bottom, south_bottom, east_bottom], base * 0.48, 0.35)

    if studs and half_width >= 14 and half_depth >= 9:
        rx = max(3, min(18, int(round(half_width * 0.14))))
        ry = max(2, min(12, int(round(half_depth * 0.20))))
        rise = max(2, min(6, int(round(height * 0.11))))
        # Four studs lie in the plane of the diamond rather than screen rows.
        centers = []
        for u, v in ((0.32, 0.32), (0.32, 0.68), (0.68, 0.32), (0.68, 0.68)):
            centers.append((int(round(cx + half_width * (u - v))),
                            int(round(cy - half_depth + half_depth * (u + v)))))
        for sx, sy in sorted(centers, key=lambda p: p[1]):
            contact = _ellipse(shape, (sx + 1, sy + 1), (rx + 1, ry + 1))
            _over(canvas, alpha, base * 0.45, contact, 0.30)
            body = _ellipse(shape, (sx, sy), (rx, ry))
            cv2.rectangle(body, (sx - rx, sy - rise), (sx + rx, sy), 255, -1)
            cv2.ellipse(body, (sx, sy - rise), (rx, ry), 0, 0, 360, 255, -1, cv2.LINE_AA)
            _over(canvas, alpha, base * 0.90 + 4.0, body)
            top = _ellipse(shape, (sx, sy - rise), (rx, ry))
            stud_shade = np.clip(0.27 - (xx - sx) / max(1, rx) * 0.045, 0.17, 0.35)
            _over(canvas, alpha, base + (255 - base) * stud_shade[:, :, None], top)
            rim = np.zeros(shape, np.uint8)
            cv2.ellipse(rim, (sx, sy - rise), (rx, ry), 0, 185, 335, 255, 1, cv2.LINE_AA)
            _over(canvas, alpha, base + (255 - base) * 0.73, rim, 0.70)

    if selected:
        _stroke(canvas, alpha, [north, east, east_bottom, south_bottom,
                                west_bottom, west], np.asarray((255, 249, 195), np.float32),
                0.72, 1, True)
    return _finish(canvas, alpha, (cx, cy), "block")


@lru_cache(maxsize=SPHERE_CACHE_SIZE)
def _sphere_sprite(radius: int, color: Color, shadow: bool) -> _Sprite:
    baked = _BAKED_SPHERES.get((radius, color, shadow))
    if baked is not None:
        return baked
    padding = max(5, int(round(radius * 0.15)))
    shape = (2 * radius + 2 * padding + max(2, int(round(radius * 0.15))),
             2 * radius + 2 * padding + max(2, int(round(radius * 0.15))))
    cx = cy = padding + radius
    canvas, alpha = _canvas(shape)
    if shadow:
        shade = _ellipse(shape, (cx + int(round(radius * 0.12)),
                                cy + int(round(radius * 0.91))),
                         (radius, max(2, int(round(radius * 0.20)))))
        shade = cv2.GaussianBlur(shade, (0, 0), max(1.0, radius * 0.055))
        _over(canvas, alpha, np.asarray((13, 18, 26), np.float32), shade, 0.32)

    yy, xx = np.mgrid[:shape[0], :shape[1]].astype(np.float32)
    nx, ny = (xx - cx) / radius, (yy - cy) / radius
    squared = nx * nx + ny * ny
    nz = np.sqrt(np.maximum(0.0, 1.0 - squared))
    distance = np.sqrt(squared) * radius
    coverage = np.clip(radius + 0.6 - distance, 0.0, 1.0)
    light = np.asarray((-0.46, -0.57, 0.68), np.float32)
    light /= np.linalg.norm(light)
    diffuse = np.maximum(0.0, nx * light[0] + ny * light[1] + nz * light[2])
    half_vector = light + np.asarray((0, 0, 1), np.float32)
    half_vector /= np.linalg.norm(half_vector)
    specular = np.maximum(0.0, nx * half_vector[0] + ny * half_vector[1]
                          + nz * half_vector[2]) ** 54
    base = np.asarray(color, np.float32)
    material = base * (0.27 + 0.76 * diffuse)[:, :, None]
    # A broad reflection sits beneath a tight highlight, like polished plastic.
    broad = np.exp(-((nx + 0.31) ** 2 / 0.16 + (ny + 0.40) ** 2 / 0.10))
    material += (255 - material) * (broad * 0.14 + specular * 0.81)[:, :, None]
    fresnel = (1.0 - nz) ** 3
    rim_direction = np.clip(0.55 - 0.35 * nx - 0.15 * ny, 0.0, 1.0)
    material += np.asarray((31, 25, 20), np.float32) * (fresnel * rim_direction)[:, :, None]
    _over(canvas, alpha, np.clip(material, 0, 255),
          np.rint(coverage * 255).astype(np.uint8))
    return _finish(canvas, alpha, (cx, cy), "sphere")


def _blend(image: np.ndarray, sprite: _Sprite, point: Tuple[int, int]) -> Bounds:
    if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Sprites require an H x W x 3 BGR uint8 image")
    height, width = image.shape[:2]
    left, top = point[0] - sprite.anchor[0], point[1] - sprite.anchor[1]
    right, bottom = left + sprite.rgb.shape[1], top + sprite.rgb.shape[0]
    x1, y1, x2, y2 = max(0, left), max(0, top), min(width, right), min(height, bottom)
    if x1 >= x2 or y1 >= y2:
        return 0, 0, 0, 0
    sx1, sy1 = x1 - left, y1 - top
    sx2, sy2 = sx1 + x2 - x1, sy1 + y2 - y1
    destination = image[y1:y2, x1:x2]
    # Only this clipped rectangle allocates working memory. The cached 16-bit
    # factors sum to 256, so opaque and transparent pixels blend exactly.
    working = destination.astype(np.uint16)
    np.multiply(working, sprite.inverse[sy1:sy2, sx1:sx2, None], out=working)
    np.add(working, sprite.premultiplied[sy1:sy2, sx1:sx2], out=working)
    np.right_shift(working, 8, out=working)
    destination[:] = working.astype(np.uint8)
    return x1, y1, x2, y2


def draw_block(image: np.ndarray, top_center: Sequence[float], half_width: float,
               half_depth: float, height: float, color: Sequence[int],
               selected: bool = False, studs: bool = True, shadow: bool = True) -> Bounds:
    """Draw a bevelled toy brick; dimensions are rounded and safely capped."""
    sprite = _block_sprite(_dimension(half_width, MAX_HALF_WIDTH),
                           _dimension(half_depth, MAX_HALF_DEPTH),
                           _dimension(height, MAX_BLOCK_HEIGHT), _color(color),
                           bool(selected), bool(studs), bool(shadow))
    return _blend(image, sprite, _point(top_center))


def draw_sphere(image: np.ndarray, center: Sequence[float], radius: float,
                color: Sequence[int], shadow: bool = True) -> Bounds:
    """Draw a lit polished sphere; the visual radius stays independent of physics."""
    sprite = _sphere_sprite(_dimension(radius, MAX_SPHERE_RADIUS), _color(color), bool(shadow))
    return _blend(image, sprite, _point(center))


def cache_info() -> Dict[str, object]:
    """Return cache hits, misses and hard limits for performance reports."""
    sprites = list(_LIVE_SPRITES.values())
    blocks = dict(_block_sprite.cache_info()._asdict())
    spheres = dict(_sphere_sprite.cache_info()._asdict())
    blocks["current_bytes"] = sum(sprite.nbytes for sprite in sprites if sprite.kind == "block")
    spheres["current_bytes"] = sum(sprite.nbytes for sprite in sprites if sprite.kind == "sphere")
    return {
        "blocks": blocks,
        "spheres": spheres,
        "current_bytes": blocks["current_bytes"] + spheres["current_bytes"],
        "pixel_buffers_upper_bound_bytes": 23432832 + _ASSET_RECORD["pixel_buffer_bytes"],
        "assets": dict(_ASSET_RECORD),
        "dimension_caps": {
            "block_half_width": MAX_HALF_WIDTH, "block_half_depth": MAX_HALF_DEPTH,
            "block_height": MAX_BLOCK_HEIGHT, "sphere_radius": MAX_SPHERE_RADIUS,
        },
        "blend": "clipped ROI uint16; no frame-sized overlay or runtime blur",
    }


def _spec_key(kind: str, spec: Dict[str, object]) -> tuple:
    if not isinstance(spec, dict):
        raise ValueError("Sprite specifications must be dictionaries")
    if kind == "block":
        required = {"half_width", "half_depth", "height", "color"}
        allowed = required | {"selected", "studs", "shadow"}
        caps = (("half_width", MAX_HALF_WIDTH), ("half_depth", MAX_HALF_DEPTH),
                ("height", MAX_BLOCK_HEIGHT))
    else:
        required = {"radius", "color"}
        allowed = required | {"shadow"}
        caps = (("radius", MAX_SPHERE_RADIUS),)
    if not required.issubset(spec) or not set(spec).issubset(allowed):
        raise ValueError("Invalid {} specification fields".format(kind))
    dimensions = []
    for field, maximum in caps:
        value = float(spec[field])
        if not math.isfinite(value) or value < 3 or value > maximum:
            raise ValueError("{} must be between 3 and {}".format(field, maximum))
        dimensions.append(_dimension(value, maximum))
    for flag in ("selected", "studs", "shadow"):
        if flag in spec and not isinstance(spec[flag], bool):
            raise ValueError("{} must be a boolean".format(flag))
    color = _color(spec["color"])
    if kind == "block":
        return tuple(dimensions) + (color, spec.get("selected", False),
                                     spec.get("studs", True), spec.get("shadow", True))
    return tuple(dimensions) + (color, spec.get("shadow", True))


def _key_spec(kind: str, key: Sequence[object]) -> Dict[str, object]:
    if not isinstance(key, list):
        raise ValueError("An asset key must be a JSON list")
    if kind == "block" and len(key) == 7:
        return {"half_width": key[0], "half_depth": key[1], "height": key[2],
                "color": key[3], "selected": key[4], "studs": key[5], "shadow": key[6]}
    if kind == "sphere" and len(key) == 3:
        return {"radius": key[0], "color": key[1], "shadow": key[2]}
    raise ValueError("Malformed {} asset key".format(kind))


def _expected_geometry(kind: str, key: tuple) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    if kind == "block":
        half_width, half_depth, height = key[:3]
        padding = max(7, int(round(half_width * 0.12)))
        dx = max(2, int(round(half_width * 0.07)))
        dy = max(3, int(round(half_depth * 0.15)))
        return ((2 * half_depth + height + 2 * padding + dy,
                 2 * half_width + 2 * padding + dx),
                (padding + half_width, padding + half_depth))
    radius = key[0]
    padding = max(5, int(round(radius * 0.15)))
    side = 2 * radius + 2 * padding + max(2, int(round(radius * 0.15)))
    return (side, side), (padding + radius, padding + radius)


def export_assets(path: object, block_specs: Sequence[Dict[str, object]],
                  sphere_specs: Sequence[Dict[str, object]]) -> Dict[str, object]:
    """Bake a bounded sprite bank on a PC; specifications match the draw APIs.

    The NPZ contains only uint8 BGR/alpha arrays and an UTF-8 JSON index encoded
    as uint8. It contains no Python objects and is loaded with allow_pickle=False.
    Duplicate normalized keys share one sprite entry.
    """
    block_keys = list(dict.fromkeys(_spec_key("block", spec) for spec in block_specs))
    sphere_keys = list(dict.fromkeys(_spec_key("sphere", spec) for spec in sphere_specs))
    if len(block_keys) > BLOCK_CACHE_SIZE or len(sphere_keys) > SPHERE_CACHE_SIZE:
        raise ValueError("Baked assets exceed the 16 block / 8 sphere entry limits")
    arrays = {}
    index = {"format": _ASSET_FORMAT, "blocks": [], "spheres": []}
    pixel_bytes = 0
    for kind, keys, builder in (("block", block_keys, _block_sprite),
                                 ("sphere", sphere_keys, _sphere_sprite)):
        for number, key in enumerate(keys):
            sprite = builder(*key)
            prefix = "{}_{}".format(kind, number)
            arrays[prefix + "_rgb"] = sprite.rgb
            arrays[prefix + "_alpha"] = sprite.alpha
            index[kind + "s"].append({"key": key, "anchor": sprite.anchor,
                                      "rgb": prefix + "_rgb", "alpha": prefix + "_alpha"})
            pixel_bytes += sprite.nbytes
    encoded = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    arrays["index"] = np.frombuffer(encoded, dtype=np.uint8)
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as stream:
        np.savez_compressed(stream, **arrays)
    return {"source": str(output), "format": _ASSET_FORMAT,
            "preloaded_blocks": len(block_keys), "preloaded_spheres": len(sphere_keys),
            "preloaded_entries": len(block_keys) + len(sphere_keys),
            "pixel_buffer_bytes": pixel_bytes, "file_bytes": output.stat().st_size}


def preload_assets(path: object) -> Dict[str, object]:
    """Replace the baked bank after validating every entry and its exact geometry.

    Loading performs integer reconstruction only. Invalid assets raise a clear
    startup error instead of silently invoking expensive sprite generation.
    """
    global _BAKED_BLOCKS, _BAKED_SPHERES, _ASSET_RECORD
    source = Path(path).resolve()
    loaded = {"block": {}, "sphere": {}}
    try:
        with np.load(str(source), allow_pickle=False) as archive:
            raw_index = archive["index"]
            if raw_index.dtype != np.uint8 or raw_index.ndim != 1 or raw_index.size > 65536:
                raise ValueError("The index must be at most 64 KiB of uint8 UTF-8 JSON")
            index = json.loads(raw_index.tobytes().decode("utf-8"))
            if not isinstance(index, dict) or set(index) != {"format", "blocks", "spheres"}:
                raise ValueError("Malformed asset index")
            if index["format"] != _ASSET_FORMAT:
                raise ValueError("Unsupported asset format")
            expected_names = {"index"}
            for kind, limit in (("block", BLOCK_CACHE_SIZE), ("sphere", SPHERE_CACHE_SIZE)):
                entries = index[kind + "s"]
                if not isinstance(entries, list) or len(entries) > limit:
                    raise ValueError("Too many {} assets".format(kind))
                for number, entry in enumerate(entries):
                    if not isinstance(entry, dict) or set(entry) != {"key", "anchor", "rgb", "alpha"}:
                        raise ValueError("Malformed {} entry".format(kind))
                    key = _spec_key(kind, _key_spec(kind, entry["key"]))
                    canonical = json.loads(json.dumps(key))
                    if canonical != entry["key"] or key in loaded[kind]:
                        raise ValueError("Duplicate or noncanonical {} key".format(kind))
                    shape, anchor = _expected_geometry(kind, key)
                    if list(anchor) != entry["anchor"]:
                        raise ValueError("Incorrect {} anchor".format(kind))
                    prefix = "{}_{}".format(kind, number)
                    if entry["rgb"] != prefix + "_rgb" or entry["alpha"] != prefix + "_alpha":
                        raise ValueError("Incorrect sprite array names")
                    expected_names.update((entry["rgb"], entry["alpha"]))
                    rgb, mask = archive[entry["rgb"]], archive[entry["alpha"]]
                    if rgb.dtype != np.uint8 or mask.dtype != np.uint8:
                        raise ValueError("Sprite pixels must be uint8")
                    if rgb.shape != shape + (3,) or mask.shape != shape:
                        raise ValueError("Sprite pixel dimensions do not match geometry")
                    loaded[kind][key] = _from_pixels(rgb, mask, anchor, kind)
            if set(archive.files) != expected_names:
                raise ValueError("Unexpected or missing asset arrays")
    except Exception as error:
        raise ValueError("Invalid pseudo-3D asset file '{}': {}".format(source, error)) from error
    _block_sprite.cache_clear()
    _sphere_sprite.cache_clear()
    _BAKED_BLOCKS, _BAKED_SPHERES = loaded["block"], loaded["sphere"]
    _ASSET_RECORD = {
        "loaded": True, "source": str(source), "format": _ASSET_FORMAT,
        "preloaded_blocks": len(_BAKED_BLOCKS), "preloaded_spheres": len(_BAKED_SPHERES),
        "preloaded_entries": len(_BAKED_BLOCKS) + len(_BAKED_SPHERES),
        "pixel_buffer_bytes": sum(sprite.nbytes for bank in loaded.values() for sprite in bank.values()),
        "file_bytes": source.stat().st_size,
    }
    return dict(_ASSET_RECORD)


_DEFAULT_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "pseudo3d_720p.npz"
if _DEFAULT_ASSETS.is_file():
    preload_assets(_DEFAULT_ASSETS)
