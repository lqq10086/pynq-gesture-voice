"""Bounded RAM observations shared by both policies; detail I/O is opt-in."""
from collections import deque
from contextlib import nullcontext
import json,sys,time

INFERENCE_FIELDS=('result_id','source_monotonic','inference_start','inference_end','submit_monotonic',
    'loop_start','loop_end','wait_ms','preprocess_ms','pl_region_e2e_ms','pl_input_validation_ms',
    'pl_input_flush_ms','pl_output_invalidate_copy_ms','ps_decode_ms','postprocess_ms',
    'ai_compute_total_ms','detection_count','capture_to_inference_ms','source_to_result_ms','trace_record_ms')
DISPLAY_FIELDS=('frame_id','result_id','loop_start','loop_end','wait_ms','ui_render_ms',
    'display_total_ms','display_ack_monotonic','camera_background_age_at_hdmi_ms',
    'detection_source_age_at_hdmi_ms','mode')
DELIVERY_FIELDS=('result_id','source','completed','consume_monotonic','display_ack_monotonic','source_to_ui_ms')

class Collection:
    def __init__(self,output,policy,capacity=8192):
        if policy not in ('production','diagnostic'):raise ValueError('Unknown collection policy')
        self.output=output;self.policy=policy;self.diagnostic=policy=='diagnostic'
        self.features=dict(full_frame_json=self.diagnostic,full_inference_json=self.diagnostic,
            prescreen_scores=self.diagnostic,prescreen_logging=self.diagnostic,periodic_jpeg=self.diagnostic,raw_probes=self.diagnostic,
            detailed_memory=self.diagnostic,bounded_ram_timing=True,screen_diagnostics='INDEPENDENT')
        self.rows={k:deque(maxlen=capacity) for k in ('inference','display','delivery')}
        self.counts={k:0 for k in self.rows}
        self.storage=dict(failures=0,saved=0,last_error=None)
        self._last_warning=-float('inf')

    def detail_log(self,name):
        return (self.output/name).open('x') if self.diagnostic else nullcontext(None)

    def record(self,kind,row):
        # Each deque has one producer; export only after worker joins.
        self.rows[kind].append(row);self.counts[kind]+=1

    def storage_failure(self,path,error):
        self.storage['failures']+=1
        self.storage['last_error']=dict(kind='DIAGNOSTIC_STORAGE_ERROR',path=str(path),reason=str(error))
        now=time.monotonic()
        if now-self._last_warning>=10:
            print('DIAGNOSTIC_STORAGE_ERROR '+json.dumps(self.storage['last_error']),file=sys.stderr,flush=True)
            self._last_warning=now

    def save_image(self,path,image,parameters=None):
        import cv2
        try:
            if not cv2.imwrite(str(path),image,[] if parameters is None else parameters):
                raise OSError('cv2.imwrite returned False')
        except Exception as error:
            self.storage_failure(path,error);return False
        self.storage['saved']+=1;return True

    def export(self):
        result=dict(clock='time.monotonic',policy=self.policy,features=self.features,
            fields=dict(inference=INFERENCE_FIELDS,display=DISPLAY_FIELDS,delivery=DELIVERY_FIELDS),
            records={k:list(v) for k,v in self.rows.items()},total=self.counts,
            dropped={k:self.counts[k]-len(v) for k,v in self.rows.items()},storage=self.storage,
            semantics='Loop end includes detail I/O; threads overlap. Source is observed completed-frame time, ACK is MM2S confirmation, neither exposure nor OBS.')
        path=self.output/'performance_samples.json';temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result,separators=(',',':')));temporary.replace(path)
