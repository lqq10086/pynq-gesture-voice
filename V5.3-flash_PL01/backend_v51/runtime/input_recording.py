"""Bounded exact-source evidence; empty early frames do not use success quotas."""
import hashlib
import json
from pathlib import Path
import re
import time


class InputRecorder:
    def __init__(self, output):
        self.output=Path(output)
        self.counts={}
        self.total=0
        self.last_empty={}

    def phase(self):
        try:
            value=json.loads((self.output/'user_phase.json').read_text())
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',value['stage']):
                raise ValueError('Invalid stage')
            return value
        except FileNotFoundError:
            return {'stage':'preparation'}
        except (OSError,ValueError,KeyError):
            return {'stage':'unknown','phase_read_error':True}

    def save(self, frame, phase, result):
        detection=result.get('raw_detection')
        stage=phase['stage']
        # Manual gesture capture starts only after explicit READY confirmation.
        if phase.get('capture_enabled') is not True:
            return None
        diagnostic=phase.get('diagnostic_capture') is True
        if phase.get('manual_ready_confirmed') is True or stage=='automatic_unlabeled':
            reason='requested_'+stage
            limit=5
            if time.monotonic()-self.last_empty.get(stage,-1e9)<2:
                return None
        elif diagnostic:
            reason='diagnostic_'+stage
            limit=2
        elif detection is not None:
            reason='detected_class_'+str(detection['class_id'])
            limit=3
        else:
            reason='empty_'+stage
            limit=1 if stage=='preparation' else 2
        if self.total>=32 or self.counts.get(reason,0)>=limit:
            return None
        if (detection is None or diagnostic) and time.monotonic()-self.last_empty.get(stage,-1e9)<7:
            return None
        start=time.monotonic()
        try:
            import cv2
            folder=self.output/'inputs'
            folder.mkdir(exist_ok=True)
            name='inputs/%s_frame_%06d.png'%(stage,frame['source_frame_id'])
            path=self.output/name
            if path.exists():raise FileExistsError(name)
            bgr=frame.get('bgr')
            raw=frame.get('raw565')
            if bgr is None:
                from frame_reader import rgb565_to_bgr
                bgr=rgb565_to_bgr(raw)
            if not cv2.imwrite(str(path),bgr,[cv2.IMWRITE_PNG_COMPRESSION,1]):
                raise OSError('PNG writer failed')
            raw_file=None
            raw_sha=None
            if raw is not None:
                raw_file=name[:-4]+'.rgb565'
                raw_path=self.output/raw_file
                with raw_path.open('xb') as stream:
                    stream.write(raw.tobytes())
                raw_sha=hashlib.sha256(raw_path.read_bytes()).hexdigest()
            record=dict(version='V2.9.8_GESTURE_4CLASS',file=name,stage=stage,phase=phase,
                selection_reason=reason,source_frame_id=frame['source_frame_id'],
                source_frame_complete_monotonic=frame['source_frame_complete_monotonic'],
                inference_start=result.get('inference_start'),inference_end=result.get('inference_end'),
                raw_detection=detection,display=result.get('display'),shape=list(bgr.shape),
                raw_rgb565_file=raw_file,raw_rgb565_sha256=raw_sha,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),saved_monotonic=time.monotonic(),
                scope='Exact retained inference source; no additional VDMA capture')
            self.counts[reason]=self.counts.get(reason,0)+1
            self.total+=1
            self.last_empty[stage]=time.monotonic()
            record['write_ms']=(time.monotonic()-start)*1000
            with (self.output/'input_manifest.jsonl').open('a') as stream:
                stream.write(json.dumps(record)+'\n')
            return record
        except Exception as error:
            return dict(stage=stage,source_frame_id=frame['source_frame_id'],
                        error=repr(error),write_ms=(time.monotonic()-start)*1000)
