"""Shared CPU inference contract for v5, v6 and FastestV2 at actual 384.

Inputs are original uint8 BGR images, already mirrored where the frozen data
contains mirrored captures; this module never flips them. Native preprocessing
is retained: v5 RGB/linear letterbox114, v6 RGB/area-downsample letterbox114,
Fastest BGR/linear stretch. All use one best class per anchor and class-aware
NMS .45, pre-NMS3000, max300. Only OpenCV/NumPy are mandatory; ORT is lazy.

For a portable board directory copy this file plus inference.py and
fastest_numpy.py beside it, or pass explicit adapter paths to UnifiedRuntime.
No training source, torch, camera, PYNQ or PL imports occur here.
"""
import hashlib
import importlib.util
from pathlib import Path
import time

import cv2
import numpy as np

NAMES = ('open_palm', 'pinch_point')
PREPROCESSING = {
    'v5': 'BGR to RGB; INTER_LINEAR resize; round resized dimensions; square114 letterbox; NCHW FP32 /255; no flip',
    'v6': 'BGR to RGB; native INTER_AREA downsample/INTER_LINEAR upsample; truncate resized dimensions; square114 letterbox; NCHW FP32 /255; no flip',
    'fastest': 'BGR retained; INTER_LINEAR stretch384; NCHW FP32 /255; no flip',
}


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def load_adapter(path, name):
    path = Path(path).resolve()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def preprocess_v5(image):
    height, width = image.shape[:2]
    ratio = min(384 / height, 384 / width)
    new_width, new_height = round(width * ratio), round(height * ratio)
    dw, dh = (384 - new_width) / 2, (384 - new_height) / 2
    if (width, height) != (new_width, new_height):
        image = cv2.resize(image, (new_width, new_height), interpolation=cv2.INTER_LINEAR)
    left, right = round(dw - .1), round(dw + .1)
    top, bottom = round(dh - .1), round(dh + .1)
    image = cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    tensor = np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1)[None], dtype=np.float32) / np.float32(255.)
    # Preserve the reference project's restore_policy='yolov5', which derives
    # floating padding from the unrounded image dimensions and common ratio.
    metadata = dict(gain_x=ratio, gain_y=ratio, pad_x=(384 - width * ratio) / 2,
                    pad_y=(384 - height * ratio) / 2, width=width, height=height,
                    border_left=left, border_top=top, resized_width=new_width, resized_height=new_height)
    return tensor, metadata


def common_nms(boxes, class_scores, thresholds=(.60, .45), iou=.45, max_det=300, pre_nms=3000):
    """Choose the best class first; retain class-aware boxes in score order."""
    boxes = np.asarray(boxes, np.float32).reshape(-1, 4)
    class_scores = np.asarray(class_scores, np.float32).reshape(-1, 2)
    thresholds = np.broadcast_to(np.asarray(thresholds, np.float32), (2,))
    classes = class_scores.argmax(1)
    scores = class_scores[np.arange(len(class_scores)), classes]
    valid = (np.isfinite(boxes).all(1) & np.isfinite(class_scores).all(1) &
             (boxes[:, 2] > boxes[:, 0]) & (boxes[:, 3] > boxes[:, 1]) &
             (scores >= thresholds[classes]) & (scores >= 0) & (scores <= 1))
    boxes, scores, classes = boxes[valid], scores[valid], classes[valid]
    order = np.argsort(-scores, kind='stable')[:pre_nms]
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    kept = []
    while len(order) and len(kept) < max_det:
        current = order[0]
        kept.append(current)
        rest = order[1:]
        intersection_wh = np.maximum(0., np.minimum(boxes[current, 2:], boxes[rest, 2:]) -
                                     np.maximum(boxes[current, :2], boxes[rest, :2]))
        intersection = intersection_wh[:, 0] * intersection_wh[:, 1]
        overlap = intersection / np.maximum(areas[current] + areas[rest] - intersection, 1e-9)
        order = rest[(overlap <= iou) | (classes[rest] != classes[current])]
    return np.ascontiguousarray(np.column_stack((boxes[kept], scores[kept], classes[kept])), dtype=np.float32)


class UnifiedRuntime:
    def __init__(self, kind, model_path, backend='onnxruntime', threads=2,
                 v6_adapter=None, fastest_adapter=None):
        if kind not in ('v5', 'v6', 'fastest'):
            raise ValueError('kind must be v5, v6 or fastest')
        if threads < 1:
            raise ValueError('threads must be positive')
        self.kind, self.model_path, self.backend, self.threads = kind, Path(model_path).resolve(), backend, threads
        self.adapter = None
        here = Path(__file__).resolve().parent
        if kind == 'v6':
            self.adapter_path = Path(v6_adapter) if v6_adapter else here / 'inference.py'
            self.adapter = load_adapter(self.adapter_path, '_comparison_v6_numpy')
            self.output_names = list(self.adapter.OUTPUT_NAMES)
        elif kind == 'fastest':
            self.adapter_path = Path(fastest_adapter) if fastest_adapter else here / 'fastest_numpy.py'
            if not self.adapter_path.is_file() and fastest_adapter is None:
                self.adapter_path = here.parents[1] / 'YOLO-FastestV2/scripts/fastest_numpy.py'
            self.adapter = load_adapter(self.adapter_path, '_comparison_fastest_numpy')
            self.output_names = ['head_s16', 'head_s32']
        else:
            self.output_names = ['output0']
        cv2.setNumThreads(threads)
        if backend == 'onnxruntime':
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            self.session = ort.InferenceSession(str(self.model_path), options, providers=['CPUExecutionProvider'])
            if len(self.session.get_inputs()) != 1 or self.session.get_inputs()[0].shape != [1, 3, 384, 384]:
                raise ValueError('CPU comparison requires actual fixed [1,3,384,384] input')
            self.input_name = self.session.get_inputs()[0].name
            if not set(self.output_names) <= {output.name for output in self.session.get_outputs()}:
                raise ValueError('Model output names differ from architecture contract')
            self.runtime_version = ort.__version__
        elif backend == 'opencv':
            self.input_name = 'images'
            self.net = cv2.dnn.readNetFromONNX(str(self.model_path))
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            if int(cv2.__version__.split('.')[0]) < 5:
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.runtime_version = cv2.__version__
        else:
            raise ValueError('backend must be onnxruntime or opencv')
        self.last_timings = {}

    def preprocess(self, image):
        if not isinstance(image, np.ndarray) or image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
            raise ValueError('Expected uint8 HWC BGR image')
        height, width = image.shape[:2]
        if self.kind == 'v5':
            return preprocess_v5(image)
        if self.kind == 'v6':
            tensor, native = self.adapter.preprocess(image)
            gx, gy, px, py, width, height = native
            return tensor, dict(gain_x=gx, gain_y=gy, pad_x=px, pad_y=py, width=width, height=height)
        tensor = np.ascontiguousarray(self.adapter.preprocess(image)[None], dtype=np.float32)
        return tensor, dict(gain_x=384 / width, gain_y=384 / height, pad_x=0., pad_y=0., width=width, height=height)

    def infer(self, tensor):
        if tensor.dtype != np.float32 or tensor.shape != (1, 3, 384, 384) or not tensor.flags.c_contiguous:
            raise ValueError('Expected contiguous FP32 [1,3,384,384] tensor')
        if self.backend == 'onnxruntime':
            return self.session.run(self.output_names, {self.input_name: tensor})
        self.net.setInput(tensor, self.input_name)
        return list(self.net.forward(self.output_names))

    def postprocess(self, outputs, metadata, thresholds=(.60, .45)):
        if self.kind == 'v6':
            values = self.adapter.decode(outputs)
            boxes, scores = values[:, :4], values[:, 4:6]
        else:
            if self.kind == 'fastest':
                values = self.adapter.decode_heads(outputs)[0]
            else:
                value = np.asarray(outputs[0])
                if value.shape != (1, 9072, 7):
                    raise ValueError('Expected v5 nc2 decoded [1,9072,7] output')
                values = value[0]
            boxes = np.concatenate((values[:, :2] - values[:, 2:4] / 2,
                                    values[:, :2] + values[:, 2:4] / 2), axis=1)
            scores = values[:, 4:5] * values[:, 5:7]
        detections = common_nms(boxes, scores, thresholds=thresholds)
        detections[:, [0, 2]] = np.clip((detections[:, [0, 2]] - metadata['pad_x']) / metadata['gain_x'], 0, metadata['width'])
        detections[:, [1, 3]] = np.clip((detections[:, [1, 3]] - metadata['pad_y']) / metadata['gain_y'], 0, metadata['height'])
        valid = (detections[:, 2] > detections[:, 0]) & (detections[:, 3] > detections[:, 1])
        return np.ascontiguousarray(detections[valid], dtype=np.float32)

    def predict(self, image, thresholds=(.60, .45)):
        start = time.perf_counter()
        tensor, metadata = self.preprocess(image)
        prepared = time.perf_counter()
        outputs = self.infer(tensor)
        inferred = time.perf_counter()
        result = self.postprocess(outputs, metadata, thresholds=thresholds)
        finished = time.perf_counter()
        self.last_timings = dict(preprocess_ms=(prepared - start) * 1000,
                                 inference_ms=(inferred - prepared) * 1000,
                                 postprocess_ms=(finished - inferred) * 1000,
                                 total_ms=(finished - start) * 1000)
        return result

    def identity(self):
        value = dict(kind=self.kind, model=str(self.model_path), model_sha256=file_sha256(self.model_path),
                     backend=self.backend, target='CPU', runtime_version=self.runtime_version, threads=self.threads,
                     opencv_version=cv2.__version__, opencv_threads=cv2.getNumThreads(), numpy_version=np.__version__,
                     input_shape=[1, 3, 384, 384], input_dtype='float32', input_name=self.input_name,
                     output_names=self.output_names, preprocessing=PREPROCESSING[self.kind],
                     classes=list(NAMES), best_class_per_anchor=True, nms_iou=.45, pre_nms_limit=3000, max_det=300,
                     runtime_source_sha256=file_sha256(__file__))
        if self.adapter is not None:
            value['adapter'] = str(self.adapter_path.resolve())
            value['adapter_sha256'] = file_sha256(self.adapter_path)
        return value
