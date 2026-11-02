"""CPU deployment preprocessing and decoding. Requires only OpenCV and NumPy."""
import cv2
import numpy as np

ANCHORS = np.array([[12.64, 19.39], [37.88, 51.48], [55.71, 138.31],
                    [126.91, 78.23], [131.57, 214.55], [279.92, 258.87]], np.float32)


def preprocess(image):
    """BGR uint8 HWC -> contiguous FP32 CHW, direct linear resize384 /255."""
    image = cv2.resize(image, (384, 384), interpolation=cv2.INTER_LINEAR)
    return np.ascontiguousarray(image.transpose(2, 0, 1), dtype=np.float32) / np.float32(255.)


def decode_heads(heads):
    """Sigmoid reg/obj, shared softmax cls input -> Bx2160x7 xywh,obj,cls2."""
    if [list(np.shape(head)[1:]) for head in heads] != [[24, 24, 17], [12, 12, 17]]:
        raise ValueError('Expected FastestV2 nc2 384 heads in stride16,32 order')
    batches = []
    for index, value in enumerate(heads):
        value = np.asarray(value, np.float32)
        batch, height, width, _ = value.shape
        regression = value[..., :12].reshape(batch, height, width, 3, 4).transpose(0, 3, 1, 2, 4)
        obj = value[..., 12:15].transpose(0, 3, 1, 2)[..., None]
        classes = np.broadcast_to(value[:, None, ..., 15:17], (batch, 3, height, width, 2))
        y, x = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')
        grid = np.stack((x, y), axis=-1).astype(np.float32)[None, None]
        xy = (regression[..., :2] * 2. - .5 + grid) * np.float32(384 / height)
        wh = (regression[..., 2:4] * 2.) ** 2 * ANCHORS[index * 3:index * 3 + 3][None, :, None, None]
        batches.append(np.concatenate((xy, wh, obj, classes), axis=-1).reshape(batch, -1, 7))
    return np.concatenate(batches, axis=1)


def box_iou(a, b):
    intersection = np.maximum(0., np.minimum(a[:, None, 2:], b[None, :, 2:]) -
                               np.maximum(a[:, None, :2], b[None, :, :2])).prod(2)
    areas_a = np.maximum(0., a[:, 2:] - a[:, :2]).prod(1)
    areas_b = np.maximum(0., b[:, 2:] - b[:, :2]).prod(1)
    return intersection / np.maximum(areas_a[:, None] + areas_b[None, :] - intersection, 1e-9)


def postprocess(rows, confidence=.001, iou=.45, max_det=300):
    """One best class per anchor; class-aware NMS -> Nx6 xyxy,score,class."""
    scores = rows[:, 4:5] * rows[:, 5:7]
    classes = scores.argmax(1)
    scores = scores[np.arange(len(rows)), classes]
    selected = scores >= confidence
    rows, classes, scores = rows[selected], classes[selected], scores[selected]
    boxes = np.concatenate((rows[:, :2] - rows[:, 2:4] / 2,
                            rows[:, :2] + rows[:, 2:4] / 2), axis=1)
    order = np.argsort(-scores, kind='stable')[:3000]
    keep = []
    while len(order) and len(keep) < max_det:
        current = order[0]
        keep.append(current)
        rest = order[1:]
        overlaps = box_iou(boxes[current:current + 1], boxes[rest])[0]
        order = rest[(overlaps <= iou) | (classes[rest] != classes[current])]
    return np.column_stack((boxes[keep], scores[keep], classes[keep])).astype(np.float32)


def restore_boxes(predictions, image_shape):
    """Map stretch384 xyxy coordinates to original HWC image coordinates."""
    result = predictions.copy()
    height, width = image_shape[:2]
    result[:, [0, 2]] *= np.float32(width / 384.)
    result[:, [1, 3]] *= np.float32(height / 384.)
    result[:, [0, 2]] = result[:, [0, 2]].clip(0, width)
    result[:, [1, 3]] = result[:, [1, 3]].clip(0, height)
    return result
