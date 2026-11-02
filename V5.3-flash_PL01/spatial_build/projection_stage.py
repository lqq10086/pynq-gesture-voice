"""Cached three-plane target stage. All target PNGs are generated offline.

This layer is only background artwork: it never changes the cube renderer's
ID/depth buffers. Draw it before the cubes. Camera changes, level changes and
new check differences rebuild the local cached warp once; ordinary frames only
blend the cached local layer into the latest live camera image.
"""
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from .model import LEVELS, WIDTH, DEPTH, MAX_HEIGHT

GAP = .22
FLOOR_Z = -2.15
STAGE_BOUNDS = ((-GAP, 0., FLOOR_Z), (float(WIDTH), DEPTH + GAP, float(MAX_HEIGHT)))


def _coordinate_hash(coords):
    value = json.dumps(sorted(tuple(c) for c in coords), separators=(",", ":"))
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def world_quad(view):
    """Texture TL,TR,BR,BL in world coordinates; testable direction contract."""
    if view == "front":
        return np.asarray(((0, DEPTH + GAP, MAX_HEIGHT), (WIDTH, DEPTH + GAP, MAX_HEIGHT),
                           (WIDTH, DEPTH + GAP, 0), (0, DEPTH + GAP, 0)), np.float64)
    if view == "left":
        return np.asarray(((-GAP, DEPTH, MAX_HEIGHT), (-GAP, 0, MAX_HEIGHT),
                           (-GAP, 0, 0), (-GAP, DEPTH, 0)), np.float64)
    if view == "top":
        return np.asarray(((0, 0, FLOOR_Z), (WIDTH, 0, FLOOR_Z),
                           (WIDTH, DEPTH, FLOOR_Z), (0, DEPTH, FLOOR_Z)), np.float64)
    raise ValueError("Unknown projection view: " + str(view))


class ProjectionStage:
    def __init__(self, root=None):
        root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
        directory = root if (root / "manifest.json").is_file() else root / "assets" / "projections"
        self.directory = directory
        self.manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if self.manifest.get("schema") != 1:
            raise ValueError("Unsupported projection asset schema")
        if self.manifest["world"] != {"width": WIDTH, "depth": DEPTH, "height": MAX_HEIGHT}:
            raise ValueError("Projection assets have different world dimensions")
        self.asset_paths = {}
        self._textures = {}
        for level in LEVELS:
            entry = self.manifest["levels"][level.id]
            if entry["coords_sha256"] != _coordinate_hash(level.coords):
                raise ValueError("Level coordinates changed; rebuild projection assets: " + level.id)
            for view in ("front", "left", "top"):
                record = entry["views"][view]
                path = directory / record["file"]
                encoded = path.read_bytes()
                if hashlib.sha256(encoded).hexdigest() != record["image_sha256"]:
                    raise ValueError("Projection PNG integrity mismatch: " + str(path))
                pixels = cv2.imdecode(np.frombuffer(encoded, np.uint8), cv2.IMREAD_UNCHANGED)
                if pixels is None or list(pixels.shape) != record["pixel_shape"] or pixels.shape[2] != 4:
                    raise ValueError("Invalid projection PNG: " + str(path))
                self.asset_paths[(level.id, view)] = path
                self._textures[(level.id, view)] = pixels
        self.warp_count = 0
        self.cache_build_count = 0
        self._cache_key = None
        self._layer = None
        self._inverse_alpha = None
        self._roi = None

    def masks(self, level_id):
        """Saved masks, no runtime target projection generation."""
        views = self.manifest["levels"][level_id]["views"]
        return {name: tuple(tuple(bool(v) for v in row) for row in views[name]["mask"])
                for name in ("front", "left", "top")}

    @staticmethod
    def _differences(result):
        if result is None:
            return ((), (), ())
        return tuple(tuple(getattr(result, name + "_differences", ()))
                     for name in ("front", "left", "top"))

    def _texture(self, level_id, view, differences):
        image = self._textures[(level_id, view)]
        if not differences:
            return image
        image = image.copy()
        record = self.manifest["levels"][level_id]["views"][view]
        rows = len(record["mask"])
        unit = self.manifest["cell_px"]
        for row, col in differences:
            display_row = rows - 1 - row if view != "top" else row
            x, y = int(col * unit), int(display_row * unit)
            cv2.rectangle(image, (x + 3, y + 3), (x + unit - 3, y + unit - 3),
                          (135, 170, 206, 240), 4, cv2.LINE_AA)
        return image

    @staticmethod
    def _over(base, layer):
        # Premultiplied floating-point BGRA, vectorized over one bounded ROI.
        # A warped hint only occupies part of the viewport. Fully transparent
        # premultiplied pixels are an exact no-op; keep the original float32
        # expression and operation order inside the occupied rectangle.
        x, y, width, height = cv2.boundingRect((layer[:, :, 3] != 0).astype(np.uint8))
        if not width or not height:
            return
        source = layer[y:y+height, x:x+width]
        target = base[y:y+height, x:x+width]
        alpha = source[:, :, 3:4]
        target[:] = source + target * (1. - alpha)

    def _build_cache(self, image, scene, level_id, differences):
        # Cache is deliberately confined to the scene viewport, not the video.
        x1, y1, x2, y2 = map(int, scene.viewport)
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(image.shape[1], x2), min(image.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Projection viewport lies outside the image")
        width, height = x2 - x1, y2 - y1
        base = np.zeros((height, width, 4), np.float32)
        origin = np.asarray((x1, y1), np.float64)
        for view, diff in (("top", differences[2]), ("front", differences[0]), ("left", differences[1])):
            source = self._texture(level_id, view, diff).astype(np.float32) / 255.
            source[:, :, :3] *= source[:, :, 3:4]
            h, w = source.shape[:2]
            src = np.asarray(((0, 0), (w - 1, 0), (w - 1, h - 1), (0, h - 1)), np.float32)
            dst = np.asarray(scene.project(world_quad(view)) - origin, np.float32)
            transform = cv2.getPerspectiveTransform(src, dst)
            layer = cv2.warpPerspective(source, transform, (width, height), flags=cv2.INTER_LINEAR,
                                        borderMode=cv2.BORDER_CONSTANT)
            self._over(base, layer)
            self.warp_count += 1
        # The legal placement floor stays at z=0, distinctly above the TOP hint.
        grid = np.zeros((height, width, 4), np.uint8)
        def polygon(points):
            return np.rint(scene.project(points) - origin).astype(np.int32)
        floor = polygon(((0, 0, 0), (WIDTH, 0, 0), (WIDTH, DEPTH, 0), (0, DEPTH, 0)))
        cv2.fillConvexPoly(grid, floor, (73, 73, 73, 110), cv2.LINE_AA)
        for x in range(WIDTH + 1):
            p = polygon(((x, 0, 0), (x, DEPTH, 0)))
            cv2.line(grid, tuple(p[0]), tuple(p[1]), (35, 35, 35, 255), 2, cv2.LINE_AA)
        for y in range(DEPTH + 1):
            p = polygon(((0, y, 0), (WIDTH, y, 0)))
            cv2.line(grid, tuple(p[0]), tuple(p[1]), (35, 35, 35, 255), 2, cv2.LINE_AA)
        cv2.polylines(grid, [floor], True, (205, 205, 205, 235), 2, cv2.LINE_AA)
        grid = grid.astype(np.float32) / 255.
        grid[:, :, :3] *= grid[:, :, 3:4]
        self._over(base, grid)
        alpha = base[:, :, 3]
        rows, cols = np.where(alpha > 0)
        if not len(rows):
            self._roi = None
            self._layer = None
            self._inverse_alpha = None
        else:
            left, right, top, bottom = cols.min(), cols.max() + 1, rows.min(), rows.max() + 1
            crop = base[top:bottom, left:right]
            self._roi = tuple(int(v) for v in (x1 + left, y1 + top, x1 + right, y1 + bottom))
            # Keep only six uint8 bytes per cached pixel. OpenCV's vectorized
            # multiply/add avoids per-frame float buffers and Python pixel work.
            self._layer = np.ascontiguousarray(np.rint(crop[:, :, :3] * 255.), dtype=np.uint8)
            inverse = np.rint((1. - crop[:, :, 3:4]) * 255.).astype(np.uint8)
            self._inverse_alpha = np.ascontiguousarray(np.repeat(inverse, 3, axis=2))
        self.cache_build_count += 1

    def coefficients(self, image, scene, level_id, result=None):
        """Return the cached local layer without reading or blending a camera."""
        if level_id not in self.manifest["levels"]:
            raise ValueError("Unknown level: " + str(level_id))
        differences = self._differences(result)
        key = (level_id, tuple(image.shape), tuple(scene.viewport), tuple(scene.pivot),
               float(scene.unit), tuple(scene.right), tuple(scene.up), differences)
        if key != self._cache_key:
            self._build_cache(image, scene, level_id, differences)
            self._cache_key = key
        return (self._roi, self._layer, self._inverse_alpha) if self._roi is not None else None

    def draw(self, image, scene, level_id, result=None):
        self.coefficients(image, scene, level_id, result)
        if self._roi is not None:
            x1, y1, x2, y2 = self._roi
            target = image[y1:y2, x1:x2]
            cv2.multiply(target, self._inverse_alpha, dst=target, scale=1. / 255.)
            cv2.add(target, self._layer, dst=target)
        return image
