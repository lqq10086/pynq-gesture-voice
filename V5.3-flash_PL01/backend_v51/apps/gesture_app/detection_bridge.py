"""Formal 384 post-NMS dictionaries to mirrored application detections."""
from .core import Detection

CLASS_NAMES = ('open_palm', 'side_palm', 'pinch_point', 'thumbs_up')
MODEL_CLASS_NAMES = ('open_palm', 'pinch_point')


def model_detections_to_app(detections, width=1280):
    result = []
    for item in detections:
        class_id = item['class_id']
        if type(class_id) is not int or not 0 <= class_id < len(MODEL_CLASS_NAMES) or item['class_name'] != MODEL_CLASS_NAMES[class_id]:
            raise ValueError('Formal 384 class index/name mismatch')
        canonical_id = CLASS_NAMES.index(MODEL_CLASS_NAMES[class_id])
        x1, y1, x2, y2 = item['source_xyxy']
        result.append(Detection(CLASS_NAMES[canonical_id], item['confidence'],
                                (width-x2, y1, width-x1, y2),
                                item.get('objectness'), item.get('class_probability')))
    return result
