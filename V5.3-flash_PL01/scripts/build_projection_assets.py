"""Offline-only creation of the nine target projection textures.

Run after editing a level: python scripts/build_projection_assets.py
The app loads the saved PNGs and masks; it never rasterizes target hints live.
"""
import hashlib
import json
from pathlib import Path
import sys

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from spatial_build.model import LEVELS, WIDTH, DEPTH, MAX_HEIGHT

CELL_PX = 64


def coordinate_hash(coords):
    payload = json.dumps(sorted(tuple(c) for c in coords), separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def texture(mask, vertical):
    """Mask z rows point upward for walls; top rows are direct world y rows."""
    mask = np.asarray(mask, dtype=bool)
    rows, columns = mask.shape
    image = np.zeros((rows * CELL_PX + 1, columns * CELL_PX + 1, 4), np.uint8)
    image[:] = (153, 153, 153, 72)
    for row in range(rows):
        image_row = rows - 1 - row if vertical else row
        for col in range(columns):
            if mask[row, col]:
                x, y = col * CELL_PX, image_row * CELL_PX
                cv2.rectangle(image, (x + 5, y + 5),
                              (x + CELL_PX - 5, y + CELL_PX - 5),
                              (228, 228, 228, 168), -1, cv2.LINE_AA)
    for col in range(columns + 1):
        cv2.line(image, (col * CELL_PX, 0), (col * CELL_PX, rows * CELL_PX),
                 (182, 182, 182, 184), 2, cv2.LINE_AA)
    for row in range(rows + 1):
        cv2.line(image, (0, row * CELL_PX), (columns * CELL_PX, row * CELL_PX),
                 (182, 182, 182, 184), 2, cv2.LINE_AA)
    return image


def build(output=None):
    output = Path(output) if output is not None else ROOT / "assets" / "projections"
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema": 1, "generated_from": "spatial_build.model.LEVELS.coords",
        "world": {"width": WIDTH, "depth": DEPTH, "height": MAX_HEIGHT},
        "cell_px": CELL_PX,
        "directions": {
            "front": "mask[z][x], bottom-first z; view from y<0 toward +y",
            "left": "mask[z][column], bottom-first z; column 0 is y=depth-1",
            "top": "mask[y][x]; row 0 is y=0, no additional mirroring"},
        "levels": {}}
    for level in LEVELS:
        entry = {"name": level.name, "coords": level.coords,
                 "coords_sha256": coordinate_hash(level.coords), "views": {}}
        for view, mask in (("front", level.front), ("left", level.left), ("top", level.top)):
            filename = "{}_{}.png".format(level.id, view)
            pixels = texture(mask, view != "top")
            encoded = cv2.imencode(".png", pixels)[1].tobytes()
            (output / filename).write_bytes(encoded)
            entry["views"][view] = {
                "file": filename, "mask": mask,
                "image_sha256": hashlib.sha256(encoded).hexdigest(),
                "pixel_shape": list(pixels.shape)}
        manifest["levels"][level.id] = entry
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
    return manifest


if __name__ == "__main__":
    data = build()
    print("Saved {} target PNGs and their coordinate/mask manifest.".format(len(data["levels"]) * 3))
