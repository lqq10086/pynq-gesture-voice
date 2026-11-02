"""Fixed-input decoded YOLOv5 FP32 inference; no PL neural network executor."""
import hashlib
import json
import time
from pathlib import Path
import cv2
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
NAMES=['open_palm','pinch_point']

def preprocess(bgr,size=384,mirror=True):
    h,w=bgr.shape[:2]
    image=cv2.flip(bgr,1) if mirror else bgr
    r=min(size/h,size/w)
    nw,nh=round(w*r),round(h*r)
    dx,dy=(size-nw)/2,(size-nh)/2
    if (w,h)!=(nw,nh):image=cv2.resize(image,(nw,nh),interpolation=cv2.INTER_LINEAR)
    image=cv2.copyMakeBorder(image,round(dy-.1),round(dy+.1),round(dx-.1),round(dx+.1),
                             cv2.BORDER_CONSTANT,value=(114,114,114))
    tensor=np.ascontiguousarray(image.transpose(2,0,1)[::-1][None],dtype=np.float32)/255.0
    return tensor,dict(width=w,height=h,scale=r,pad_x=dx,pad_y=dy,
                       resized_width=nw,resized_height=nh,inference_mirror=mirror)

def map_box(box,g,integer=True):
    x1,y1,x2,y2=[float(v) for v in box]
    x1,x2=(x1-g['pad_x'])/g['scale'],(x2-g['pad_x'])/g['scale']
    y1,y2=(y1-g['pad_y'])/g['scale'],(y2-g['pad_y'])/g['scale']
    w,h=g['width'],g['height']
    # YOLO boxes are continuous boundaries [0,W]; reverse mirror BEFORE
    # converting to the inherited PL interface's inclusive integer pixels.
    if g['inference_mirror']:x1,x2=w-x2,w-x1
    values=[max(0,min(w,x1)),max(0,min(h,y1)),max(0,min(w,x2)),max(0,min(h,y2))]
    if not integer:return values
    return [max(0,min(limit-1,round(v))) for v,limit in zip(values,[w,h,w,h])]

def _finish_trace(trace,rows,reasons,g,conf,iou,max_det,class_thresholds,enabled_classes,palm_thresholds=None):
    """Summarize masks from the actual selection; never run another NMS."""
    labels=('low_objectness','low_class_threshold','unbound_class','nms_suppressed','max_det_limited','selected')
    counts=np.bincount(reasons,minlength=6)
    trace['counts']=dict(input_rows=len(rows),objectness_candidates=len(rows)-int(counts[0]),
        low_objectness=int(counts[0]),low_class_threshold=int(counts[1]),unbound_class=int(counts[2]),
        nms_suppressed=int(counts[3]),max_det_limited=int(counts[4]),final_count=int(counts[5]))
    trace['summary_reason']='selected' if counts[5] else ('no_objectness_candidates' if counts[0]==len(rows) else 'all_candidates_filtered')
    trace['visual_reason']='NOT_DETERMINABLE_FROM_PREDICTIONS'
    trace['thresholds']=dict(objectness=conf,class_thresholds=class_thresholds,iou=iou,max_det=max_det,enabled_classes=enabled_classes)
    if palm_thresholds is not None:
        obj,score=palm_thresholds
        thresholds=list(class_thresholds) if class_thresholds is not None else [None]*(rows.shape[1]-5)
        thresholds[0]=score
        trace['thresholds'].update(palm_thresholds=[obj,score],class_thresholds=thresholds,
            objectness_by_class=[obj]+[conf]*(rows.shape[1]-6),
            score_floor_by_class=[min(obj,conf)]+[conf]*(rows.shape[1]-6))
    if trace.get('level')!='detailed':return
    scores=rows[:,4:5]*rows[:,5:]
    winners=scores.argmax(1);confidence=scores.max(1)
    trace['class_max_scores']=scores.max(0).tolist() if len(rows) else [0.]*(rows.shape[1]-5)
    trace['winning_class_max_scores']=[float(confidence[winners==k].max()) if np.any(winners==k) else 0. for k in range(scores.shape[1])]
    if trace.get('level')=='detailed':
        candidates=[]
        limit=4 if counts[0]==len(rows) else 64
        for k in np.argsort(-confidence,kind='stable')[:limit]:
            box=np.concatenate((rows[k,:2]-rows[k,2:4]/2,rows[k,:2]+rows[k,2:4]/2))
            candidates.append(dict(candidate_id=int(k),class_id=int(winners[k]),confidence=float(confidence[k]),
                objectness=float(rows[k,4]),class_probabilities=rows[k,5:].tolist(),
                scores=scores[k].tolist(),reason=labels[int(reasons[k])],ai_xyxy=box.tolist(),source_xyxy=map_box(box,g,False)))
        trace['candidates']=candidates;trace['candidate_dump_omitted']=max(0,len(rows)-len(candidates))
        trace['coordinates']='ai_xyxy: mirrored letterbox input; source_xyxy: original unmirrored camera pixels'

def postprocess(output,g,conf=.60,iou=.45,max_det=1000,names=None,*,timings=None,enabled_classes=None,class_thresholds=None,trace=None,palm_thresholds=None):
    if timings is not None:started=time.monotonic()
    p=np.asarray(output)[0]
    if p.ndim!=2 or p.shape[1]!=5+len(NAMES if names is None else names):raise ValueError('Expected decoded [1,N,5+nc] output')
    objectness=p[:,4]>conf
    all_scores=None
    if palm_thresholds is not None:
        values=np.asarray(palm_thresholds,dtype=np.float64)
        if values.shape!=(2,) or not np.isfinite(values).all() or np.any((values<0)|(values>1)):
            raise ValueError('Expected finite palm objectness/score thresholds in [0,1]')
        if (NAMES if names is None else names)[0]!='open_palm':raise ValueError('Palm thresholds require class zero open_palm')
        palm_thresholds=tuple(float(v) for v in values)
        all_scores=p[:,5:]*p[:,4:5]
        palm=all_scores.argmax(1)==0
        objectness[palm]=p[palm,4]>palm_thresholds[0]
    if trace is not None:
        rows=p;row_ids=np.flatnonzero(objectness);reasons=np.zeros(len(p),np.uint8);reasons[row_ids]=1
    p=p[objectness]
    if not len(p):
        if trace is not None:_finish_trace(trace,rows,reasons,g,conf,iou,max_det,class_thresholds,enabled_classes,palm_thresholds)
        if timings is not None:
            elapsed=(time.monotonic()-started)*1000
            timings.update(candidate_prepare_ms=elapsed,nms_core_ms=0.,
                           detection_mapping_ms=0.,detector_postprocess_ms=elapsed)
        return []
    scores=p[:,5:]*p[:,4:5] if all_scores is None else all_scores[objectness]
    classes=scores.argmax(1);confidence=scores.max(1)
    keep=confidence>conf
    if palm_thresholds is not None:
        palm=classes==0
        keep[palm]=(confidence[palm]>min(palm_thresholds[0],conf)) & (confidence[palm]>=palm_thresholds[1])
    if class_thresholds is not None:
        thresholds=np.asarray(class_thresholds,dtype=confidence.dtype)
        if thresholds.shape!=(p.shape[1]-5,) or not np.isfinite(thresholds).all():
            raise ValueError('Expected one finite threshold per class')
        if palm_thresholds is not None:
            thresholds=thresholds.copy();thresholds[0]=palm_thresholds[1]
        keep &= confidence>=thresholds[classes]
    if enabled_classes is not None:
        if trace is not None:reasons[row_ids[keep]]=2
        keep &= np.isin(classes,enabled_classes)
    if trace is not None:
        row_ids=row_ids[keep];reasons[row_ids]=4
    p,classes,confidence=p[keep],classes[keep],confidence[keep]
    boxes=np.concatenate((p[:,:2]-p[:,2:4]/2,p[:,:2]+p[:,2:4]/2),axis=1)
    if timings is not None:prepared=time.monotonic()
    # NMS core includes stable score ordering and the original IoU selection.
    order=np.argsort(-confidence,kind='stable')[:30000]
    selected=[]
    while len(order) and len(selected)<max_det:
        k=int(order[0]);selected.append(k);rest=order[1:]
        if trace is not None:reasons[row_ids[k]]=5
        if not len(rest):break
        inter=np.maximum(0,np.minimum(boxes[k,2:],boxes[rest,2:])-np.maximum(boxes[k,:2],boxes[rest,:2])).prod(1)
        area=np.maximum(0,boxes[:,2:]-boxes[:,:2]).prod(1)
        overlap=inter/(area[k]+area[rest]-inter+1e-7)
        survive=(classes[rest]!=classes[k])|(overlap<=iou)
        if trace is not None:reasons[row_ids[rest[~survive]]]=3
        order=rest[survive]
    if timings is not None:suppressed=time.monotonic()
    names=NAMES if names is None else names
    detections=[dict(class_id=int(classes[k]),class_name=names[int(classes[k])],
                     objectness=float(p[k,4]),class_probability=float(p[k,5+classes[k]]),
                     confidence=float(confidence[k]),ai_xyxy=boxes[k].tolist(),
                     source_xyxy=map_box(boxes[k],g,False),video_xyxy=map_box(boxes[k],g)) for k in selected]
    if trace is not None:_finish_trace(trace,rows,reasons,g,conf,iou,max_det,class_thresholds,enabled_classes,palm_thresholds)
    if timings is not None:
        mapped=time.monotonic()
        timings.update(candidate_prepare_ms=(prepared-started)*1000,
                       nms_core_ms=(suppressed-prepared)*1000,
                       detection_mapping_ms=(mapped-suppressed)*1000,
                       detector_postprocess_ms=(mapped-started)*1000)
    return detections

class MiniModel:
    def __init__(self,config=None):
        self.config=json.loads(Path(config or ROOT/'config/model.json').read_text(encoding='utf-8-sig'))
        c=self.config
        self.path=(ROOT/c['model_path']).resolve()
        self.sha256=hashlib.sha256(self.path.read_bytes()).hexdigest()
        if self.sha256!=c['sha256']:raise RuntimeError('Model identity changed: validate updated weights first')
        cv2.setNumThreads(c['threads'])
        start=time.monotonic()
        self.net=cv2.dnn.readNetFromONNX(str(self.path))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self.load_ms=(time.monotonic()-start)*1000

    def infer(self,bgr,return_output=False):
        c=self.config;t0=time.monotonic()
        tensor,g=preprocess(bgr,c['input_shape'][2],c['inference_mirror']);t1=time.monotonic()
        self.net.setInput(tensor);t2=time.monotonic()
        output=self.net.forward();t3=time.monotonic()
        if list(output.shape)!=c['output_shape'] or not np.isfinite(output).all():
            raise RuntimeError('Invalid model output')
        detections=postprocess(output,g,c['confidence'],c['iou'],c['max_det'],c['classes'])
        highest=(output[0,:,4:5]*output[0,:,5:]).max(0).tolist()
        t4=time.monotonic()
        result=dict(detections=detections,geometry=g,preprocess_ms=(t1-t0)*1000,
                    setInput_ms=(t2-t1)*1000,network_forward_ms=(t3-t2)*1000,
                    postprocess_ms=(t4-t3)*1000,ai_total_ms=(t4-t0)*1000,
                    prescreen_max_scores=highest,inference_start=t0,inference_end=t4)
        return (result,output) if return_output else result

    def identity(self):
        return dict(version='V2.9.8_GESTURE_4CLASS',neural_network_backend='PS_OpenCV_DNN_FP32',
                    pl_conv_acceleration=False,opencv=cv2.__version__,threads=cv2.getNumThreads(),
                    model_sha256=self.sha256,network_load_ms=self.load_ms,config=self.config)
