"""Fixed YOLOv5n tensor contract and PS postprocessing, without PyTorch."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path

import cv2
import numpy as np

from .core import Detection

NAMES_3 = ("open_palm", "side_palm", "pinch_point")
NAMES_4 = NAMES_3 + ("thumbs_up",)
INPUT_SHAPE = (1, 3, 224, 224)


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
    """Input is already mirrored. Return RGB FP32 NCHW and letterbox geometry."""
    if (mirrored_bgr.dtype != np.uint8 or mirrored_bgr.ndim != 3
            or mirrored_bgr.shape[2] != 3 or min(mirrored_bgr.shape[:2]) < 1):
        raise ValueError("Expected nonempty uint8 HWC BGR frame")
    height, width = mirrored_bgr.shape[:2]
    ratio = min(224 / height, 224 / width)
    new_width, new_height = round(width * ratio), round(height * ratio)
    dw, dh = (224 - new_width) / 2, (224 - new_height) / 2
    image = mirrored_bgr
    if (width, height) != (new_width, new_height):
        image = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    image = cv2.copyMakeBorder(image, top, bottom, left, right,
                               cv2.BORDER_CONSTANT, value=(114, 114, 114))
    tensor = np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32)
    tensor /= np.float32(255.0)
    return tensor, Geometry(width, height, ratio, dw, dh, left, top)


def validate_input(tensor):
    if tensor.dtype != np.float32 or tensor.shape != INPUT_SHAPE or not tensor.flags.c_contiguous:
        raise ValueError("Accelerator input must be contiguous float32 [1,3,224,224]")
    if not np.isfinite(tensor).all() or tensor.min() < 0 or tensor.max() > 1:
        raise ValueError("Accelerator input must contain finite RGB values in [0,1]")


def validate_output(raw, class_count):
    raw = np.asarray(raw)
    expected = (1, 3087, 5 + class_count)
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


def decode(raw, geometry, names=NAMES_3, confidence=0.60, pinch_confidence=0.45,
           iou=0.45, max_det=20, restore_policy="yolov5"):
    """One best class per row; class-aware NMS; restore mirrored image coordinates.

    'yolov5' reproduces predecessor scale_boxes(shape, shape) for odd dimensions.
    'letterbox' uses stored dw/dh, matching the earlier board reference script.
    """
    if not (0 < confidence <= 1 and 0 < pinch_confidence <= 1 and 0 < iou <= 1 and max_det >= 1):
        raise ValueError("Invalid postprocess settings")
    prediction = validate_output(raw, len(names))[0]
    minimum = min(confidence, pinch_confidence)
    prediction = prediction[prediction[:, 4] > minimum]
    scores_all = prediction[:, 4:5] * prediction[:, 5:]
    classes = np.argmax(scores_all, axis=1)
    scores = scores_all[np.arange(len(prediction)), classes]
    mask = scores > minimum
    prediction, classes, scores = prediction[mask], classes[mask], scores[mask]
    boxes = np.column_stack((prediction[:, :2] - prediction[:, 2:4] / 2,
                              prediction[:, :2] + prediction[:, 2:4] / 2))
    # Match YOLOv5's pre-NMS limit and order.
    order = np.argsort(-scores, kind="stable")[:30000]
    boxes, classes, scores = boxes[order], classes[order], scores[order]
    if restore_policy == "yolov5":
        pad = ((224 - geometry.width * geometry.ratio) / 2,
               (224 - geometry.height * geometry.ratio) / 2)
    elif restore_policy == "letterbox":
        pad = (geometry.pad_x, geometry.pad_y)
    else:
        raise ValueError("Unknown restore_policy")
    result = []
    for index in _nms(boxes, scores, classes, iou, max_det):
        name, score = names[int(classes[index])], float(scores[index])
        if score < (pinch_confidence if name == "pinch_point" else confidence):
            continue
        box = boxes[index].copy()
        box[[0, 2]] = np.clip((box[[0, 2]] - pad[0]) / geometry.ratio, 0, geometry.width)
        box[[1, 3]] = np.clip((box[[1, 3]] - pad[1]) / geometry.ratio, 0, geometry.height)
        result.append(Detection(name, score, tuple(int(v) for v in np.rint(box))))
    return result


class OpenCvRunner:
    def __init__(self, model, class_count=3):
        model = Path(model)
        if not model.is_file():
            raise FileNotFoundError("ONNX model not found: %s" % model)
        # Bytes support Unicode paths on Windows and OpenCV 4.5.4.
        self.net = cv2.dnn.readNetFromONNX(np.frombuffer(model.read_bytes(), dtype=np.uint8))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.class_count = class_count

    def infer(self, tensor):
        validate_input(tensor)
        self.net.setInput(tensor, "images")
        return validate_output(self.net.forward("output0"), self.class_count)

    def close(self):
        pass


class OrtRunner:
    def __init__(self, model, class_count=3):
        import onnxruntime as ort
        self.session = ort.InferenceSession(str(model), providers=["CPUExecutionProvider"])
        self.class_count = class_count
        if self.session.get_inputs()[0].name != "images" or self.session.get_inputs()[0].shape != list(INPUT_SHAPE):
            raise ValueError("Unexpected ONNX input contract")

    def infer(self, tensor):
        validate_input(tensor)
        return validate_output(self.session.run(["output0"], {"images": tensor})[0], self.class_count)

    def close(self):
        pass


def load_factory(spec):
    module, separator, name = spec.partition(":")
    if not separator or not module or not name:
        raise ValueError("Plugin factory must be module:function")
    return getattr(importlib.import_module(module), name)


def create_runner(config, root):
    backend = config.get("backend", "opencv")
    names = tuple(config.get("names", NAMES_3))
    if names not in (NAMES_3, NAMES_4):
        raise ValueError("Expected documented 3-class or 4-class order")
    if backend in ("opencv", "ort"):
        path = Path(config["model"])
        if not path.is_absolute():
            path = Path(root) / path
        runner = (OpenCvRunner if backend == "opencv" else OrtRunner)(path, len(names))
    elif backend == "pynq_dma":
        if names != NAMES_3:
            raise ValueError("FP32 DMA example requires the 3-class contract")
        from .pynq_dma import PynqDmaRunner
        runner = PynqDmaRunner(config["dma"])
    elif backend == "plugin":
        runner = load_factory(config["factory"])(config.get("options", {}))
    else:
        raise ValueError("Unknown inference backend: %s" % backend)
    return runner, names
