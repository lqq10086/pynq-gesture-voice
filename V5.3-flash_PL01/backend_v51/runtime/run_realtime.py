"""V2.9.7-mini: PS reference or full integer PL graph, same safe video owner."""
import argparse
import json
import os
import signal
import threading
import time
import traceback
import sys
from pathlib import Path

import cv2
import numpy as np

from camera_control import CameraControlSession
from display_owner import DisplayOwner
from frame_reader import CompletedFrameReader
from shutdown_owner import shutdown_retaining
from pipeline import MiniPipeline
from hardware_idle import read_idle_kernel,require_pynq_device

ROOT=Path(__file__).resolve().parent
STATUS=ROOT/'realtime_status.json'


def check_overlay_ip(camera):
    expected={'bbox_shadow_gpio0':0x41260000,'bbox_shadow_gpio1':0x41270000,
              'bbox_ack_gpio':0x41280000}
    for name,address in expected.items():
        entry=camera.overlay.ip_dict.get(name)
        if not entry or int(entry['phys_addr'])!=address:
            raise RuntimeError('Bbox MMIO missing or misplaced: '+name)


def acquire_owner():
    from owner_guard import acquire_owner as acquire
    return acquire()


def mark_quiescent(camera, executor):
    from owner_guard import mark_quiescent as mark
    return mark(camera,executor)


def read_video_health(camera):
    status=camera.get_status()
    live=status['live']
    mm2s=int(camera.vdma_mmio.read(4))
    s2mm=int(camera.vdma_mmio.read(0x34))
    required=('clock_locked','config_done','video_running')
    errors=('config_error','fifo_overflow','address_overflow',
            'underflow','sync_error','sensor_error')
    # Same implemented VDMA error bits as board_static_test.check_video().
    mm2s_errors,s2mm_errors=mm2s&0x770,s2mm&0x770
    healthy=(all(live[name] for name in required) and
             not any(live[name] for name in errors) and
             not (mm2s_errors or s2mm_errors or ((mm2s|s2mm)&1)))
    return dict(status=status,live=live,mm2s=mm2s,s2mm=s2mm,
                mm2s_error_bits=mm2s_errors,s2mm_error_bits=s2mm_errors,
                healthy=healthy,sampled_monotonic=time.monotonic())


def write_status(result, status_path=None):
    target=Path(status_path) if status_path is not None else STATUS
    temporary=target.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,indent=2))
    temporary.replace(target)


def run(max_frames=None, timeout_s=2.0, *, source_max_age_s=10.0,
        display_mode='raw', smooth_alpha=.5, bitstream=None, output_dir=None, record_user_inputs=False,
        package=None,mode='ps_only'):
    output=Path(output_dir or ROOT).resolve()
    output.mkdir(parents=True,exist_ok=True)
    samples_path=output/'v297_mini_live_samples.jsonl'
    # Atomically reserve one batch before constructing or touching hardware.
    # Even an empty previous run is evidence and requires a new output folder.
    with samples_path.open('x'):
        pass
    status_path=output/'realtime_status.json'
    display_status_path=output/'realtime_display_status.json'
    recorder=None
    if record_user_inputs:
        from input_recording import InputRecorder
        recorder=InputRecorder(output)
    def save_status(state):
        write_status(state,status_path)
    def save_sample(state):
        with samples_path.open('a') as history:
            history.write(json.dumps({key:value for key,value in state.items()
                                      if key!='suffix_layer_profile'})+'\n')
    cv2.setNumThreads(2)
    camera=CameraControlSession()
    executor=None
    display=None
    owner_lock=None
    sample_pending=False
    stop_requested=False
    wake=threading.Event()
    def defer_signal(signum,frame):
        nonlocal stop_requested
        stop_requested=True
        wake.set()
        print('Stop requested; retaining ownership until inference and DMA are idle',flush=True)
    signal.signal(signal.SIGTERM,defer_signal)
    signal.signal(signal.SIGINT,defer_signal)
    paths=dict(bitstream=str(Path(bitstream).resolve()))
    model=MiniPipeline(package,mode)
    result={'status':'STARTING','version':'V2.9.8_GESTURE_4CLASS','frames_processed':0,
            'run_started_unix_s':time.time(),'owner_pid':os.getpid(),
            'detections':0,'box_commits':0,
            'frame_copy_drops':0,'paths':paths,'display_mode':display_mode,
            'source_max_age_s':source_max_age_s,'display_hold_s':timeout_s,
            'latency_origin':'Observed S2MM pointer transition proxy; not sensor exposure',
            'frame_id_semantics':'Unique observed completion IDs; skipped sensor frames uncounted',
            'output_dir':str(output),'status_file':str(status_path),
            'samples_file':str(samples_path),'display_status_file':str(display_status_path),
            'hardware_qualification':'NOT_IMPLIED_BY_RUNTIME_VERSION'}
    result.update(model.identity())
    try:
        result['device_preflight']=require_pynq_device()
        owner_lock=acquire_owner()
        camera.load_overlay(paths['bitstream'])
        check_overlay_ip(camera)
        result['unused_kernel_ap_ctrl']=read_idle_kernel(camera)
        display=DisplayOwner(hold_s=timeout_s,source_max_age_s=source_max_age_s,
                             mode=display_mode,alpha=smooth_alpha,
                             status_path=str(display_status_path),
                             events_path=str(output/'display_events.jsonl'))
        display.start()
        try:
            model.attach(camera)
        finally:
            # A failed attachment may already own CMA; retain it for safe cleanup.
            executor=model.executor
        result.update(model.identity())
        reader=CompletedFrameReader(camera)
        previous_ai=None
        while not stop_requested and (max_frames is None or result['frames_processed']<max_frames):
            phase=recorder.phase() if recorder else None
            result['video']=read_video_health(camera)
            if not result['video']['healthy']:
                raise RuntimeError('Video electronic status or VDMA error before frame capture')
            try:
                frame=reader.read(timeout=2.0)
            except RuntimeError as error:
                if 'stable VDMA buffer window' not in str(error): raise
                result['frame_copy_drops']+=1
                if result['frame_copy_drops']>=10 and result['frames_processed']==0:
                    raise RuntimeError('Unable to copy one stable camera frame') from error
                continue
            start=time.monotonic()
            cpu_start=time.process_time()
            prediction=model.infer(frame)
            detections=prediction['detections']
            detected=prediction['inference_end']
            result['process_cpu_percent_of_2_cores']=100*(time.process_time()-cpu_start)/(2*(detected-start))
            result['frames_processed']+=1
            sample_pending=True
            result['ai_inference_ms']=prediction['ai_total_ms']
            result['ai_compute_only_ms']=(detected-start)*1000
            if previous_ai is not None:
                result['ai_update_fps']=1/(detected-previous_ai)
                result['result_update_period_ms']=(detected-previous_ai)*1000
            previous_ai=detected
            result.update({k:v for k,v in prediction.items() if k!='detections'})
            result.update(video_capture_fps_proxy=None,capture_fps_status=frame['capture_fps_status'],
                          capture_copy_ms=frame['copy_ms'])
            for key in ('wait_transition_ms','copy_guard_ms','rgb565_to_bgr_ms','frame_read_total_ms'):
                result[key]=frame[key]
            source=frame['source_frame_complete_monotonic']
            raw=None
            if detections:
                raw=dict(detections[0])
                result['detections']+=1
            message=dict(source_frame_id=frame['source_frame_id'],
                         source_frame_complete_monotonic=source,
                         inference_start=start,inference_end=detected,detection=raw)
            # Source timing belongs to this result, and survives IPC unchanged.
            result.update(message)
            result['raw_detection']=raw
            result['all_detections']=detections
            if recorder:result['user_phase']=phase
            result['overlay_commit_ack_monotonic']=None
            result['time_since_commit_ms']=None
            result['camera_to_overlay_latency_ms']=None
            result['result_source_age_ms']=(time.monotonic()-source)*1000
            result['frame_completion_to_detection_ms']=(detected-source)*1000
            result['camera_to_detection_latency_ms']=(detected-source)*1000
            result['video']=read_video_health(camera)
            if not result['video']['healthy']:
                raise RuntimeError('Video electronic status or VDMA error during AI inference')
            if stop_requested:
                result['display_submit_skipped']='stop_requested'
                result['status']='STOP_REQUESTED'
                save_sample(result)
                sample_pending=False
                break
            submit_started=time.monotonic()
            shown=display.submit(message)
            result['display_submit_started_monotonic']=submit_started
            result['display_submit_roundtrip_ms']=(time.monotonic()-submit_started)*1000
            action='box' if shown.get('new_detection') else ('clear' if shown.get('accepted') and shown.get('clear_reason')=='empty_result' else 'none')
            ack_time=shown.get('overlay_commit_ack_monotonic') if action=='box' else shown.get('clear_ack_monotonic') if action=='clear' else None
            result['display_submit_action']=action
            result['display_submit_to_ack_ms']=(ack_time-submit_started)*1000 if ack_time is not None else None
            result['display']=shown
            result['box_commits']=shown['box_commits']
            result['box_valid']=shown['box_valid']
            # A clear/rejection must never inherit an older result's commit latency.
            committed=shown.get('new_detection',False)
            result['overlay_commit_ack_monotonic']=shown['overlay_commit_ack_monotonic'] if committed else None
            result['result_source_age_ms']=(time.monotonic()-source)*1000
            result['time_since_commit_ms']=shown['time_since_commit_ms'] if committed else None
            result['camera_to_overlay_latency_ms']=shown.get('frame_completion_to_overlay_ms') if committed else None
            result['status']='RUNNING_CAMERA_AI_OVERLAY'
            if recorder:
                result['input_capture']=recorder.save(frame,phase,result)
            save_status(result)
            save_sample(result)
            sample_pending=False
            print('frame=%d class=%s infer_ms=%.1f source_age_ms=%.1f display=%s' %
                  (frame['source_frame_id'],raw['class_name'] if raw else 'none',
                   result['ai_inference_ms'],result['result_source_age_ms'],
                   'committed' if committed else shown.get('reject_reason','cleared')),flush=True)
        result['status']='STOP_REQUESTED' if stop_requested else 'FRAME_LIMIT_REACHED'
    except BaseException:
        result['status']='FAILED'
        result['error']=traceback.format_exc()
        print(result['error'],flush=True)
        if sample_pending:
            save_sample(result)
    finally:
        # No overlay reload on uncertain DMA ownership. Failed stops retain this
        # process and all allocations, retry with diagnostics, and accept SIGTERM.
        if owner_lock is not None:
            shutdown_retaining(camera,executor,display,result,save_status,wake)
        else:
            save_status(result)
        if owner_lock is not None:
            try:
                result['handoff']=mark_quiescent(camera,executor)
            except Exception:
                result['status']='FAILED'
                result['handoff']={'state':'UNKNOWN','error':traceback.format_exc()}
            finally:
                owner_lock.close()
            save_status(result)
    return result

