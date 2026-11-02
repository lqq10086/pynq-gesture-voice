"""Bounded evidence from the existing owner's exact inference input."""
import hashlib
import json
from pathlib import Path
import re
import time


class InputRecorder:
    def __init__(self,output):
        self.output=Path(output)
        self.counts={}
        self.total=0

    def phase(self):
        path=self.output/'user_phase.json'
        if not path.exists():return {'stage':'preparation'}
        try:
            value=json.loads(path.read_text())
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,48}',value['stage']):raise ValueError('Invalid stage')
            return value
        except (OSError,ValueError,KeyError):return {'stage':'unknown','phase_read_error':True}

    def save(self,frame,phase,result):
        stage=phase['stage'];limit=1 if stage=='preparation' else 3
        if self.total>=32 or self.counts.get(stage,0)>=limit:return None
        start=time.monotonic()
        try:
            import cv2
            folder=self.output/'inputs';folder.mkdir(exist_ok=True)
            name='inputs/%s_frame_%06d.png'%(stage,frame['source_frame_id'])
            path=self.output/name
            if path.exists():raise FileExistsError(name)
            if not cv2.imwrite(str(path),frame['bgr'],[cv2.IMWRITE_PNG_COMPRESSION,1]):
                raise OSError('PNG writer failed')
            record={'file':name,'stage':stage,'phase':phase,
                'source_frame_id':frame['source_frame_id'],
                'source_frame_complete_monotonic':frame['source_frame_complete_monotonic'],
                'inference_start':result.get('inference_start'),'inference_end':result.get('inference_end'),
                'raw_detection':result.get('raw_detection'),'display':result.get('display'),
                'shape':list(frame['bgr'].shape),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                'saved_monotonic':time.monotonic(),
                'scope':'Exact BGR source used by this inference; saved after submission, no new capture'}
            with (self.output/'input_manifest.jsonl').open('a') as out:
                out.write(json.dumps(record)+'\n')
            self.counts[stage]=self.counts.get(stage,0)+1;self.total+=1
            record['write_ms']=(time.monotonic()-start)*1000
            return record
        except Exception as error:
            return {'stage':stage,'source_frame_id':frame['source_frame_id'],
                    'error':repr(error),'write_ms':(time.monotonic()-start)*1000}


class EventJournal:
    def __init__(self,path):
        self.path=Path(path);self.previous=None

    def record(self,state):
        key=tuple(state.get(k) for k in ('box_commits','clear_commits',
            'latest_result_source_frame_id','display_error','stop_reason'))
        if key==self.previous:return
        with self.path.open('a') as out:out.write(json.dumps(state)+'\n')
        self.previous=key
