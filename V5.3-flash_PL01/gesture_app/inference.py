"""Native two-head FastestV2 CPU tensor contract and PS postprocessing."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path

import numpy as np

from .core import Detection

NAMES_2 = ("open_palm", "pinch_point")
# Retained for comparison scripts and the original synthetic diagnostics only.
NAMES_3 = ("open_palm", "side_palm", "pinch_point")
NAMES_4 = NAMES_3 + ("thumbs_up",)
INPUT_SIZE = 384
PREDICTION_COUNT = 2160
INPUT_SHAPE = (1, 3, INPUT_SIZE, INPUT_SIZE)


@dataclass(frozen=True)
class Geometry:
    width: int
    height: int
    ratio: float
    pad_x: float
    pad_y: float
    border_left: int
    border_top: int


def preprocess(mirrored_bgr):
    """Already mirrored/corrected BGR -> native stretch384 FP32 NCHW."""
    if (mirrored_bgr.dtype != np.uint8 or mirrored_bgr.ndim != 3
            or mirrored_bgr.shape[2] != 3 or min(mirrored_bgr.shape[:2]) < 1):
        raise ValueError("Expected nonempty uint8 HWC BGR frame")
    height, width = mirrored_bgr.shape[:2]
    from fastest_numpy import preprocess as native_preprocess
    tensor = native_preprocess(mirrored_bgr)[None]
    return tensor, Geometry(width, height, INPUT_SIZE / width, 0., 0., 0, 0)


def validate_input(tensor):
    if tensor.dtype != np.float32 or tensor.shape != INPUT_SHAPE or not tensor.flags.c_contiguous:
        raise ValueError("Accelerator input must be contiguous float32 %s" % (INPUT_SHAPE,))
    if not np.isfinite(tensor).all() or tensor.min() < 0 or tensor.max() > 1:
        raise ValueError("CPU model input must contain finite BGR values in [0,1]")


def validate_output(raw, class_count):
    raw = np.asarray(raw)
    expected = (1, PREDICTION_COUNT, 5 + class_count)
    if raw.dtype != np.float32 or raw.shape != expected:
        raise ValueError("Decoded accelerator output must be float32 %s, got %s %s. "
                         "Do not truncate a 4-class head or return convolution logits."
                         % (expected, raw.shape, raw.dtype))
    if not np.isfinite(raw).all():
        raise ValueError("Accelerator output contains non-finite values")
    if np.any(raw[..., 2:4] < 0) or np.any(raw[..., 4:] < -1e-6) or np.any(raw[..., 4:] > 1.000001):
        raise ValueError("Expected decoded positive widths/heights and sigmoid probabilities")
    return raw


def _nms(boxes, scores, classes, iou, max_det):
    order = np.argsort(-scores, kind="stable")
    area = np.maximum(0, boxes[:, 2] - boxes[:, 0]) * np.maximum(0, boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size and len(keep) < max_det:
        selected = int(order[0])
        keep.append(selected)
        rest = order[1:]
        lo = np.maximum(boxes[selected, :2], boxes[rest, :2])
        hi = np.minimum(boxes[selected, 2:], boxes[rest, 2:])
        intersection = np.prod(np.maximum(0, hi - lo), axis=1)
        overlap = intersection / np.maximum(area[selected] + area[rest] - intersection, 1e-9)
        order = rest[(classes[rest] != classes[selected]) | (overlap <= iou)]
    return keep


def decode(raw, geometry, names=NAMES_2, confidence=0.60, pinch_confidence=0.45,
           iou=0.45, max_det=20, restore_policy="stretch", enabled_names=NAMES_2):
    """One best class per row; class-aware NMS; restore mirrored image coordinates.

    Native FastestV2 uses separate x/y stretch gains and no padding.
    """
    if not (0 < confidence <= 1 and 0 < pinch_confidence <= 1 and 0 < iou <= 1 and max_det >= 1):
        raise ValueError("Invalid postprocess settings")
    prediction = validate_output(raw, len(names))[0]
    minimum = min(confidence, pinch_confidence)
    prediction = prediction[prediction[:, 4] >= minimum]
    scores_all = prediction[:, 4:5] * prediction[:, 5:]
    classes = np.argmax(scores_all, axis=1)
    scores = scores_all[np.arange(len(prediction)), classes]
    # Choose the best class across the complete model head BEFORE filtering.
    allowed = np.array([name in enabled_names for name in names], dtype=bool)
    thresholds = np.array([pinch_confidence if name == 'pinch_point' else confidence for name in names], np.float32)
    mask = (scores >= thresholds[classes]) & allowed[classes]
    prediction, classes, scores = prediction[mask], classes[mask], scores[mask]
    boxes = np.column_stack((prediction[:, :2] - prediction[:, 2:4] / 2,
                              prediction[:, :2] + prediction[:, 2:4] / 2))
    order = np.argsort(-scores, kind="stable")[:3000]
    boxes, classes, scores = boxes[order], classes[order], scores[order]
    if restore_policy != 'stretch':
        raise ValueError("Unknown restore_policy")
    result = []
    for index in _nms(boxes, scores, classes, iou, max_det):
        name, score = names[int(classes[index])], float(scores[index])
        box = boxes[index].copy()
        box[[0, 2]] = np.clip(box[[0, 2]] * (geometry.width / INPUT_SIZE), 0, geometry.width)
        box[[1, 3]] = np.clip(box[[1, 3]] * (geometry.height / INPUT_SIZE), 0, geometry.height)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        result.append(Detection(name, score, tuple(int(v) for v in np.rint(box))))
    return result


class OpenCvRunner:
    backend = 'opencv'

    def __init__(self, model, class_count=2, threads=2):
        from comparison_runtime import UnifiedRuntime
        if class_count != 2:
            raise ValueError('Fastest deployment has exactly two classes')
        self.runtime = UnifiedRuntime('fastest', model, backend=self.backend, threads=threads)

    def infer(self, tensor):
        validate_input(tensor)
        from fastest_numpy import decode_heads
        return validate_output(decode_heads(self.runtime.infer(tensor)), 2)

    def close(self):
        pass


class OrtRunner(OpenCvRunner):
    backend = 'onnxruntime'


def load_factory(spec):
    module, separator, name = spec.partition(":")
    if not separator or not module or not name:
        raise ValueError("Plugin factory must be module:function")
    return getattr(importlib.import_module(module), name)


def create_runner(config, root):
    backend = config.get("backend", "opencv")
    names = tuple(config.get("names", NAMES_2))
    if names != NAMES_2:
        raise ValueError("Expected two-class Fastest order open_palm, pinch_point")
    if backend in ("opencv", "ort"):
        path = Path(config["model"])
        if not path.is_absolute():
            path = Path(root) / path
        runner = (OpenCvRunner if backend == "opencv" else OrtRunner)(path, len(names), config.get("threads", 4))
    elif backend == "pynq_dma":
        from .pynq_dma import PynqDmaRunner
        runner = PynqDmaRunner(config["dma"])
    elif backend == "plugin":
        runner = load_factory(config["factory"])(config.get("options", {}))
    else:
        raise ValueError("Unknown inference backend: %s" % backend)
    return runner, names
