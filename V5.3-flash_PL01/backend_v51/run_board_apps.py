"""Real OV5640 -> latest-frame PL detector and independent HDMI applications."""
if __name__=='__main__':
    import sys
    if not any(arg in ('--help','-h') for arg in sys.argv[1:]):
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parent/'runtime'))
        from owner_guard import run_entry_locked
        run_entry_locked(__file__)
        raise SystemExit(0)
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
import numpy as np

ROOT=Path(__file__).resolve().parent
RUNTIME_ROOT=ROOT/'runtime'
sys.path.insert(0,str(RUNTIME_ROOT))
sys.path.insert(0,str(ROOT/'apps'))
import cv2
from camera_control import CameraControlSession
from frame_reader import CompletedFrameReader
from hardware_idle import require_pynq_device
from owner_guard import acquire_owner,mark_quiescent
from pipeline import MiniPipeline
from region_executor import check_global_addresses
from run_realtime import read_video_health
from gesture_app.board_app import AsyncGestureApplication
from gesture_app.detection_bridge import model_detections_to_app
from gesture_app.detection_overlay import draw_application_detections
from hdmi_output import HdmiOutput
from frame_color import raw565_to_mirrored_bgr,native_used
from collection import Collection,INFERENCE_FIELDS,DISPLAY_FIELDS,DELIVERY_FIELDS
from video_statistics import VideoStatistics,snapshot_rates
from wb_maintenance import OwnerMaintenance,owner_identity


def atomic_json(path,value):
    temporary=path.with_suffix('.tmp')
    try:
        temporary.write_text(json.dumps(value,indent=2))
        temporary.replace(path)
    except OSError as error:
        print('STATUS_WRITE_FAILED path='+str(path)+' reason='+repr(error),file=sys.stderr,flush=True)
        raise


def interaction_state(app):
    hub=app.hub
    state=dict(mode=hub.mode,zoom=round(app.state.zoom,4),resizing=hub.resizing,
               pointer=hub.pointer,latched=hub._latched)
    game=hub.game
    if game is not None:
        state['game']={k:sorted(v) if isinstance(v,set) else v.copy() if isinstance(v,list) else v
                       for k,v in vars(game).items()
                       if k in ('board','score','matched','flips','moves','message','finished',
                                'hits','misses','combo','round','pairs','streak','lives',
                                'revealed','turns','hide_at','phase','best','target')}
        if isinstance(state['game'].get('board'),dict):
            # JSON object keys encode (column,row); detach stacks from live play.
            state['game']['board']={f'{col},{row}':stack.copy()
                                    for (col,row),stack in sorted(game.board.items())}
        if hasattr(game,'started_at'):
            state['game']['remaining_s']=max(0,round(30-(time.perf_counter()-game.started_at)))
    return state


def display_ready(frame,last_frame):
    return frame is not None and frame['source_frame_id']!=last_frame


def runtime_policy(mode,reference_only=False):
    if mode=='hybrid' and not reference_only:return 'FORMAL_PL'
    if mode=='ps_only' and reference_only:return 'B384_PS_FP32_REFERENCE_ONLY'
    raise ValueError('Formal V3.2 requires real PL; PS is allowed only in the explicit reference entry')


def memory_snapshot():
    process={line.split(':',1)[0]:line.split(':',1)[1].strip()
             for line in Path('/proc/self/status').read_text().splitlines()
             if line.startswith(('VmRSS:','VmHWM:'))}
    process['cma_meminfo']=[line for line in Path('/proc/meminfo').read_text().splitlines()
                            if line.startswith(('CmaTotal:','CmaFree:'))]
    return process


class PLCameraDisplay:
    """Read only the frozen PL's post-CCM HDMI timing/mirror monitor.

    Timing frames can repeat camera content. UI rejection is expected while
    the independent UI VDMA remains halted; it does not stall the camera.
    """
    def __init__(self,camera):
        root=ET.parse(Path(camera.bit_path).with_suffix('.hwh')).getroot()
        descriptions=[]
        for name in ('ui_status_gpio0','ui_status_gpio1'):
            modules=[m for m in root.findall('.//MODULE') if m.get('INSTANCE')==name]
            ranges={(int(m.get('BASEVALUE'),0),int(m.get('HIGHVALUE'),0))
                    for m in root.findall('.//MEMRANGE') if m.get('INSTANCE')==name
                    and m.get('SLAVEBUSINTERFACE')=='S_AXI'}
            entry=camera.overlay.ip_dict.get(name,{})
            if (len(modules)!=1 or modules[0].get('VLNV')!='xilinx.com:ip:axi_gpio:2.0'
                    or len(ranges)!=1):
                raise RuntimeError('HWH missing qualified PL HDMI status GPIO: '+name)
            base,high=next(iter(ranges))
            if (int(entry.get('phys_addr',-1))!=base or int(entry.get('addr_range',0))!=high-base+1
                    or entry.get('type')!='xilinx.com:ip:axi_gpio:2.0'):
                raise RuntimeError('PL HDMI status mapping differs from HWH: '+name)
            descriptions.append(entry)
        self.status0,self.status1=[camera._make_mmio(entry) for entry in descriptions]
        initial=self._read_status()
        self._last_count=initial['video_count']
        self._last_at=self._last_advance=time.monotonic()
        self.total=0

    def _read_status(self):
        for _ in range(8):
            word=int(self.status0.read(0))
            presented=int(self.status0.read(8))
            video=int(self.status1.read(0));rejected=int(self.status1.read(8))
            if word==int(self.status0.read(0)) and presented==int(self.status0.read(8)):
                if word>>24!=0x45:raise RuntimeError('PL HDMI monitor identity mismatch')
                if word&0x10000:raise RuntimeError('PL camera mirror synchronization failed')
                return dict(video_count=video,mirror_error=False,
                    ui_presented_count=presented,ui_presented_tag=word&65535,
                    ui_rejected_frames=rejected)
        raise RuntimeError('PL HDMI status did not stabilize')

    def sample(self,now):
        result=self._read_status()
        delta=(result['video_count']-self._last_count)&0xffffffff
        if delta>=0x80000000:raise RuntimeError('PL HDMI timing counter regressed')
        if delta:self._last_advance=now
        if now-self._last_advance>2.:raise RuntimeError('PL HDMI timing counter stalled')
        elapsed=now-self._last_at
        self.total+=delta
        self._last_count=result['video_count'];self._last_at=now
        return dict(result,hdmi_timing_frames=self.total,
                    hdmi_refresh_fps=delta/elapsed if elapsed>0 else None,
                    healthy=True,sampled_monotonic=now,
                    timing_boundary='Post-CCM last active HDMI pixel; timing frames may repeat camera content; not OBS',
                    ui_rejection_expected=True)


def run(output,duration=None,mode='hybrid',initial_mode='home',diagnostics=False,glass_quality='glass_fast',reference_only=False,reference_bit=None,collection_policy='production',gesture_trace='off',trace_images_every=0,detection_hud=False,detection_profile='baseline',voice_mode='exclusive',display_path='pl-camera'):
    if display_path!='pl-camera':raise ValueError('This candidate supports pl-camera only; use V5.0 for UI')
    execution_role=runtime_policy(mode,reference_only)
    if reference_bit is not None and not reference_only:raise ValueError('Bit override is reference-only')
    bit=Path(reference_bit) if reference_bit else ROOT/'runtime/hardware/camera_v32_384.bit'
    output.mkdir(parents=True,exist_ok=True)
    collection=Collection(output,collection_policy)
    collection.features['prescreen_scores']=collection.diagnostic or bool(diagnostics)
    collection.features['screen_diagnostics']=bool(diagnostics)
    stop=threading.Event()
    shared=threading.Condition()
    latest={'frame':None,'error':None,'prediction':None,'captured':0,'copy_drops':0,
            'probe_until':0.,'probe_next':0.,'gesture_test_until':0.}
    status=dict(status='STARTING',version='V5.1_30p',owner_pid=os.getpid(),
                runtime_variant='V5.1_30p',display_path='PL_CAMERA_NO_UI',ui_rendering=False,
                display_frame_semantics='HDMI timing frames, may repeat source; NOT unique camera FPS',
                camera_image_fps_proxy=None,latest_ai_compute_ms=None,
                client_date='2026-10-16',board_unix_s=time.time(),output_dir=str(output),
                obs_status='PENDING_HUMAN_CONFIRMATION',real_gesture_acceptance='PENDING',
                confirmation_policy=dict(click_fresh_results=1,release_fresh_results=1,
                    held_pinch_repeats=False,mode_transition_preserves_latch=True),
                inference_frames=0,display_frames=0,
                execution_role=execution_role,
                latency_revision='V3.2_384_SPATIAL4',
                collection_policy=collection_policy,collection_features=collection.features,
                gesture_trace_mode=gesture_trace,trace_images_every=trace_images_every,
                diagnostic_storage=collection.storage,
                latency_origin='Observed S2MM frame-store transition, not sensor exposure or OBS timing')
    status_path=output/'status.json'
    artifacts=[ROOT/'model_package'/name for name in ('model_manifest.json','reference.onnx',
               'layer_config.json','weights_padded.bin','bias_int64.bin','requant_int32.bin')]
    artifacts += [bit,bit.with_suffix('.hwh'),ROOT/'layer_dataflow_plan.json']
    status['artifact_identity']={str(p):dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in artifacts}
    status['memory_before']=memory_snapshot()
    atomic_json(status_path,status)
    def request_stop(signum,frame):
        stop.set()
        with shared:shared.notify_all()
    signal.signal(signal.SIGTERM,request_stop)
    signal.signal(signal.SIGINT,request_stop)
    camera=CameraControlSession()
    app=AsyncGestureApplication(mode=initial_mode,stale_s=0.9,visual_quality=glass_quality)
    status.update(diagnostics=bool(diagnostics),glass_quality=glass_quality,detection_hud=bool(detection_hud))
    try:
        model=MiniPipeline(ROOT/'model_package',mode,detection_profile=detection_profile)
        if mode=='hybrid':
            from qualification import require_numeric
            status['numeric_qualification_sha256']=require_numeric(ROOT,model.decoder_identity)
    except BaseException as error:
        status.update(status='FAILED_BEFORE_OWNERSHIP',error=repr(error),shutdown_verified=False)
        atomic_json(status_path,status)
        raise
    pipeline_module=sys.modules[MiniPipeline.__module__]
    original_postprocess=pipeline_module.postprocess
    last_postprocess_times={}
    def measured_postprocess(*args,**kwargs):
        before=time.monotonic()
        last_postprocess_times.clear()
        kwargs['timings']=last_postprocess_times
        result=original_postprocess(*args,**kwargs)
        last_postprocess_times['detector_postprocess_ms']=(time.monotonic()-before)*1000
        return result
    pipeline_module.postprocess=measured_postprocess
    cv2.setNumThreads(1)
    owner=None
    hdmi=None
    pl_display=None
    trace=None
    voice=None
    workers=[]
    maintenance=None
    phase='initial_APPS'
    started=time.monotonic()
    snapshot_requested=True
    try:
        status['devices']=require_pynq_device()
        owner=acquire_owner()
        camera.load_overlay(bit,output_factory=None if display_path=='pl-camera' else HdmiOutput)
        if display_path=='pl-camera':pl_display=PLCameraDisplay(camera)
        else:hdmi=camera.hdmi_output
        model.attach(camera)
        check_global_addresses([owner for owner in (camera,model.executor,hdmi) if owner is not None])
        status.update(model.identity())
        status['version']='V5.1_30p'
        status['module_origins']={name:str(Path(module.__file__).resolve()) for name,module in list(sys.modules.items())
            if getattr(module,'__file__',None) and name in ('pipeline','package_contract','quantized_decode','region_executor','collection','camera_control','owner_guard')}
        status['thresholds']=dict(model.config['detection_thresholds'],detection_profile=detection_profile)
        status['native_color_conversion']=native_used
        status['independent_output_addresses']=[int(b.device_address) for b in hdmi.buffers] if hdmi is not None else []
        status['capture_addresses']=[int(b.device_address) for b in camera.buffers]
        status['owned_buffer_bytes']=dict(camera=sum(b.nbytes for b in camera.buffers),
            hdmi=sum(b.nbytes for b in hdmi.buffers) if hdmi is not None else 0,
            ai=sum(b.nbytes for b in model.executor.buffers) if model.executor else 0)
        status['clocks']=camera._check_clocks()
        statistics_reader=None if reference_only else VideoStatistics(camera.control_mmio)
        previous_hardware=None if statistics_reader is None else statistics_reader.snapshot()
        axi_clock_hz=next(item['mhz'] for item in status['clocks']['fclk'] if item['index']==0)*1e6
        status['hardware_statistics']=previous_hardware
        if statistics_reader is not None and execution_role=='FORMAL_PL':
            maintenance=OwnerMaintenance(output,owner_identity(ROOT,output,status))
            status['wb_maintenance']=maintenance.performance_metadata()
        status['clock_implementations']={name:vars(time.get_clock_info(name)) for name in ('monotonic','perf_counter')}
        if gesture_trace!='off':
            from gesture_trace import GestureTrace
            trace=GestureTrace(output/'gesture_trace',mode=gesture_trace,image_every=trace_images_every,
                identity=dict(model=model.identity(),artifacts=status['artifact_identity'],
                    runtime_qualification_sha256=model.qualification and hashlib.sha256((ROOT/'runtime/runtime_qualification.json').read_bytes()).hexdigest(),
                    boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                    input_size=[720,1280],model_input_shape=[1,3,384,384],
                    frame_time_origin='Software observed complete S2MM frame, not exposure',
                    app_coordinates='Mirrored camera pixels; source images are unmirrored RGB565'))
            app.set_trace(lambda event:trace.emit(event.get('kind','app_event'),event,monotonic=event.get('monotonic')))
        reader=CompletedFrameReader(camera)
        display_background=np.empty((720,1280,3),np.uint8) if display_path!='pl-camera' else None
        from voice_bridge import VoiceBridge
        voice=VoiceBridge(app,ROOT,mode=voice_mode)
        status['voice']=voice.snapshot()
        cpu_last=(time.monotonic(),time.process_time())
        def fail(error):
            with shared:
                latest['error']=repr(error)
                stop.set()
                shared.notify_all()
            print(traceback.format_exc(),flush=True)
        def capture():
            try:
                while not stop.is_set():
                    try:frame=reader.read(timeout=2)
                    except RuntimeError as error:
                        if 'stable VDMA buffer window' not in str(error):raise
                        with shared:latest['copy_drops']+=1
                        continue
                    with shared:
                        frame['capture_publish_monotonic']=time.monotonic()
                        latest['frame']=frame
                        latest['captured']+=1
                        shared.notify_all()
            except BaseException as error:fail(error)
        def inference():
            last=-1
            number=0
            try:
                with collection.detail_log('inference_samples.jsonl') as log:
                    while not stop.is_set():
                        loop_start=time.monotonic()
                        with shared:
                            shared.wait_for(lambda:stop.is_set() or latest['frame'] is not None and
                                            latest['frame']['source_frame_id']!=last,timeout=1)
                            if stop.is_set():break
                            frame=latest['frame']
                            if frame is None or frame['source_frame_id']==last:continue
                            last=frame['source_frame_id']
                        wait_end=time.monotonic()
                        if voice.should_pause_gesture or not voice.gate.try_begin_gesture():
                            stop.wait(.01)
                            continue
                        try:
                            prediction=model.infer(frame,collect_scores=collection.diagnostic or diagnostics,
                                                   trace_level=gesture_trace if trace is not None else None)
                        finally:
                            voice.gate.end_gesture()
                        prediction.update(last_postprocess_times)
                        number+=1
                        prediction.update(result_id=number,source_frame_id=last,
                            source_monotonic=frame['source_frame_complete_monotonic'],
                            capture_publish_monotonic=frame['capture_publish_monotonic'],
                            capture_to_inference_ms=(prediction['inference_start']-frame['source_frame_complete_monotonic'])*1000,
                            ready_to_inference_ms=(prediction['inference_start']-frame['capture_publish_monotonic'])*1000,
                            source_to_result_ms=(prediction['inference_end']-frame['source_frame_complete_monotonic'])*1000)
                        detections=model_detections_to_app(prediction['detections'],
                                                           width=prediction['geometry']['width'])
                        source_image=(raw565_to_mirrored_bgr(frame['raw565'])
                                      if display_path!='pl-camera' and any(d.label=='side_palm' for d in detections) else None)
                        prediction['submit_monotonic']=time.monotonic()
                        trace_started=time.monotonic() if trace is not None else None
                        if trace is not None:
                            prediction.update(frame_id=last,application_name=app.hub.mode,
                                app_detections=[dict(label=d.label,confidence=d.confidence,box=d.box) for d in detections])
                            trace.record_inference(prediction,frame=frame if trace.wants_image(number) else None)
                        if display_path!='pl-camera':
                            app.submit_detections(detections,number,prediction['source_monotonic'],
                                                  prediction['inference_end'],source_image=source_image,frame_id=last)
                        with shared:
                            latest['prediction']=prediction
                            shared.notify_all()
                        if trace is not None:
                            trace.emit('publish',dict(result_id=number,frame_id=last,
                                source_monotonic=prediction['source_monotonic'],published_monotonic=time.monotonic()))
                            prediction['trace_record_ms']=(time.monotonic()-trace_started)*1000
                        with shared:
                            capture_sample=(collection.diagnostic and prediction['inference_end']<latest['probe_until'] and
                                            prediction['inference_end']>=latest['probe_next'])
                            if capture_sample:latest['probe_next']=prediction['inference_end']+1
                            gesture_preview=collection.diagnostic and prediction['inference_end']<latest['gesture_test_until']
                        if capture_sample:
                            name='probe_%05d'%number
                            try:
                                frame['raw565'].tofile(output/(name+'.rgb565'))
                                atomic_json(output/(name+'.json'),prediction)
                            except OSError as error:collection.storage_failure(output/name,error)
                        log_record=dict(prediction) if log is not None else None
                        if gesture_preview:
                            preview_started=time.monotonic()
                            packed=np.asarray(frame['raw565']).reshape(720,1280,2)[::2,::2]
                            preview=cv2.flip(cv2.cvtColor(packed,cv2.COLOR_BGR5652BGR),1)
                            name='gesture_%05d_camera.jpg'%number
                            saved=collection.save_image(output/name,preview,[cv2.IMWRITE_JPEG_QUALITY,85])
                            log_record.update(gesture_preview=name if saved else None,
                                preview_io_ms=(time.monotonic()-preview_started)*1000,
                                preview_clock_origin='S2MM transition timestamp of this exact inference input')
                        if log is not None:
                            log.write(json.dumps(log_record)+'\n')
                            log.flush()
                        timing=dict(prediction,loop_start=loop_start,loop_end=time.monotonic(),
                            wait_ms=(wait_end-loop_start)*1000,detection_count=len(detections))
                        collection.record('inference',tuple(timing.get(k) for k in INFERENCE_FIELDS))

            except BaseException as error:fail(error)
        status['startup_s']=time.monotonic()-started
        # A bounded acceptance run measures active camera/display time. Slow
        # initialization must not consume the requested live observation window.
        started=time.monotonic()
        for name,function in [('capture',capture),('inference',inference)]:
            worker=threading.Thread(name=name,target=function,daemon=False)
            workers.append(worker)
            worker.start()
        status['status']='RUNNING_REAL_CAMERA_HDMI_APPS'
        status['video']=read_video_health(camera)
        atomic_json(status_path,status)
        last_frame=-1
        last_status=last_snapshot=last_health=0.
        last_ui=None
        request_mtime=None
        last_maintenance_signature=None
        def health_tick(now):
            # This runs even while no new frame is available. Controlled WB
            # transitions never relax the capture worker's existing 2s timeout.
            nonlocal last_health,previous_hardware,last_maintenance_signature
            if now-last_health<1:return
            status['video']=read_video_health(camera);last_health=now
            if pl_display is not None:
                status['pl_camera_display']=pl_display.sample(now)
                status['hdmi_timing_frames']=status['display_frames']=pl_display.total
                status['hdmi_refresh_fps']=status['pl_camera_display']['hdmi_refresh_fps']
            if statistics_reader is not None:
                current_hardware=statistics_reader.snapshot()
                status['hardware_statistics']=current_hardware
                status['hardware_rates']=snapshot_rates(previous_hardware,current_hardware,axi_clock_hz)
                status['camera_image_fps_proxy']=status['hardware_rates']['s2mm_complete_hz']
                status['camera_image_fps_proxy_semantics']='Unique completed camera S2MM updates; not HDMI/OBS uniqueness'
                def wb_endpoint():
                    return {key:int(camera.control_mmio.read(offset)) for key,offset in
                            [('abi',0x18),('status',0x1c),('accepted',0x30),('completed',0x34),('flags',0x3c)]}
                if maintenance is None:
                    healthy=not any(status['hardware_rates'][key] for key in
                        ('dvp_drop_events','fifo_overflow_events','dvp_axis_errors','s2mm_axis_errors','s2mm_axi_errors'))
                    if not healthy:raise RuntimeError('Live hardware statistics reported errors')
                    decision=dict(hardware_healthy=True,classification='STEADY',transition=False)
                else:
                    try:
                        decision=maintenance.evaluate(previous_hardware,current_hardware,status['hardware_rates'],
                                                       status['video'],wb_endpoint)
                    except Exception as error:
                        status['hardware_healthy']=False
                        status['hardware_health_classification']='FAILED'
                        status['wb_maintenance']=dict(error=str(error),**maintenance.performance_metadata())
                        raise
                status['hardware_healthy']=decision['hardware_healthy']
                status['hardware_health_classification']=decision['classification']
                if maintenance is not None:
                    status['wb_maintenance']=dict(current=decision,**maintenance.performance_metadata())
                previous_hardware=current_hardware
                signature=(decision.get('active_nonce'),decision.get('completed_intervals'),decision['classification'])
                if decision['transition'] or signature!=last_maintenance_signature:
                    # Raw counters/rates remain intact and this status is
                    # explicitly not steady-state video acceptance.
                    atomic_json(status_path,status)
                last_maintenance_signature=signature
            if not status['video']['healthy']:raise RuntimeError('Live video health failed')
        with collection.detail_log('display_samples.jsonl') as display_log, collection.detail_log('interaction_events.jsonl') as event_log, collection.detail_log('result_delivery.jsonl') as delivery_log:
            while not stop.is_set() and (duration is None or time.monotonic()-started<duration):
                loop_start=time.monotonic()
                if maintenance is not None or pl_display is not None:health_tick(loop_start)
                control=output/'control.json'
                if control.exists() and control.stat().st_mtime_ns!=request_mtime:
                    request=json.loads(control.read_text())
                    request_mtime=control.stat().st_mtime_ns
                    phase=request.get('phase',phase)
                    if 'voice_listening' in request:
                        voice.set_listening(bool(request['voice_listening']))
                    if 'diagnostics' in request:
                        diagnostics=bool(request['diagnostics'])
                        status['diagnostics']=diagnostics
                        collection.features['prescreen_scores']=collection.diagnostic or diagnostics
                        collection.features['screen_diagnostics']=diagnostics
                    snapshot_requested=request.get('snapshot',True)
                    if 'capture_seconds' in request and collection.diagnostic:
                        with shared:
                            latest['probe_until']=time.monotonic()+min(60,float(request['capture_seconds']))
                            latest['probe_next']=0.
                    if 'gesture_test_seconds' in request and collection.diagnostic:
                        with shared:
                            latest['gesture_test_until']=time.monotonic()+max(0,min(120,float(request['gesture_test_seconds'])))
                with shared:
                    shared.wait_for(lambda:stop.is_set() or display_ready(latest['frame'],last_frame),timeout=1)
                    if stop.is_set():break
                    frame=latest['frame']
                    prediction=latest['prediction']
                    captured,drops=latest['captured'],latest['copy_drops']
                    if not display_ready(frame,last_frame):continue
                    last_frame=frame['source_frame_id']
                t0=time.monotonic()
                wait_ms=(t0-loop_start)*1000
                if display_path=='pl-camera':
                    voice.tick()
                    health_tick(t0)
                    if t0-last_status>=(1 if collection.diagnostic else 5):
                        cpu_now=(time.monotonic(),time.process_time())
                        status['cpu_process_percent']=100*(cpu_now[1]-cpu_last[1])/max(.001,cpu_now[0]-cpu_last[0])
                        cpu_last=cpu_now
                        status.update(phase=phase,voice=voice.snapshot(),captured_frames=captured,
                            frame_copy_drops=drops,inference_frames=prediction['result_id'] if prediction else 0,
                            latest_ai_compute_ms=prediction['ai_compute_total_ms'] if prediction else None,
                            last_prediction=prediction if collection.diagnostic else None,
                            sampled_monotonic=t0,uptime_s=t0-started,
                            memory=memory_snapshot() if collection.diagnostic else None)
                        atomic_json(status_path,status);last_status=t0
                    continue
                mirrored=raw565_to_mirrored_bgr(frame['raw565'],destination=display_background)
                t1=time.monotonic()
                voice.tick()
                rendered=app.render_frame(mirrored,inplace=True)
                rendered,overlay_info=draw_application_detections(rendered,app,hud=detection_hud,
                    details=(gesture_trace=='detailed'))
                if diagnostics and prediction:
                    scores=prediction['prescreen_max_scores']
                    pinch_score='--' if scores is None else '%.2f'%scores[model.config['classes'].index('pinch_point')]
                    label='PINCH %s / .45    UI %.1f Hz    AI %.0f ms'%(pinch_score,app.fps,
                            prediction['ai_compute_total_ms'])
                    cv2.putText(rendered,label,(18,62 if detection_hud else 30),cv2.FONT_HERSHEY_SIMPLEX,.65,
                                (246,248,250),1,cv2.LINE_AA)
                t2=time.monotonic()
                display=hdmi.write_frame(rendered)
                t3=time.monotonic()
                status['display_frames']+=1
                for result_trace in app.timings['consumed_results']:
                    delivery=dict(result_trace,phase=phase,
                        display_ack_monotonic=display['display_ack_monotonic'],
                        source_to_ui_ms=((display['display_ack_monotonic']-result_trace['source'])*1000 if result_trace['source'] is not None else None),
                        result_to_hdmi_ms=((display['display_ack_monotonic']-result_trace['completed'])*1000 if result_trace['completed'] is not None else None))
                    if delivery_log is not None:delivery_log.write(json.dumps(delivery)+'\n')
                    collection.record('delivery',tuple(delivery.get(k) for k in DELIVERY_FIELDS))
                    if trace is not None:trace.emit('display_ack',delivery)
                ui=interaction_state(app)
                state_for_event={k:v for k,v in ui.items() if k!='pointer'}
                mode_changed=last_ui is None or state_for_event['mode']!=last_ui['mode']
                if state_for_event!=last_ui:
                    event=dict(monotonic=t3,phase=phase,result_id=app.last_result_id,
                               new_model_result=app.timings['new_result'],
                               state=ui,evidence='UI_STATE_OBSERVED_WITH_REAL_CAMERA')
                    if event_log is not None:event_log.write(json.dumps(event)+'\n');event_log.flush()
                    if last_ui is None or state_for_event['mode'] != last_ui['mode'] or t3-last_snapshot>=2:
                        snapshot_requested=True
                    last_ui=state_for_event
                result_age=(None if app.source_monotonic is None else (t3-app.source_monotonic)*1000)
                sample=dict(frame_id=last_frame,phase=phase,mode=app.hub.mode,
                            result_id=app.last_result_id,camera_convert_mirror_ms=(t1-t0)*1000,
                            hud_result_id=prediction['result_id'] if prediction else None,
                            ui_render_ms=(t2-t1)*1000,display_total_ms=(t3-t0)*1000,
                            camera_background_age_at_hdmi_ms=(t3-frame['source_frame_complete_monotonic'])*1000,
                            detection_source_age_at_hdmi_ms=result_age,application=app.timings,hdmi=display)
                if display_log is not None:display_log.write(json.dumps(sample)+'\n')
                if maintenance is None:health_tick(t3)
                if mode_changed or t3-last_status>=(1 if collection.diagnostic else 5):
                    cpu_now=(time.monotonic(),time.process_time())
                    status['cpu_process_percent']=100*(cpu_now[1]-cpu_last[1])/max(.001,cpu_now[0]-cpu_last[0])
                    cpu_last=cpu_now
                    status['voice']=voice.snapshot()
                    status.update(phase=phase,interaction=ui,detection_overlay=overlay_info,
                                  captured_frames=captured,frame_copy_drops=drops,
                                  inference_frames=prediction['result_id'] if prediction else 0,
                                  last_prediction=prediction if collection.diagnostic else None,
                                  last_display=sample if collection.diagnostic else None,
                                  sampled_monotonic=t3,uptime_s=t3-started,
                                  memory=memory_snapshot() if collection.diagnostic else None)
                    if not status['video']['healthy']:raise RuntimeError('Live video health failed')
                    atomic_json(status_path,status)
                    if display_log is not None:display_log.flush()
                    if delivery_log is not None:delivery_log.flush()
                    last_status=t3
                if collection.diagnostic and (snapshot_requested or t3-last_snapshot>=5):
                    # Rendering reused this private BGR buffer. Decode the still
                    # immutable raw copy only when a camera snapshot is needed.
                    camera_snapshot=raw565_to_mirrored_bgr(frame['raw565'])
                    collection.save_image(output/'latest_camera.jpg',camera_snapshot)
                    collection.save_image(output/'latest_hdmi.jpg',rendered)
                    if snapshot_requested:
                        name='%05d_%s'%(status['display_frames'],app.hub.mode)
                        collection.save_image(output/(name+'_camera.jpg'),camera_snapshot)
                        collection.save_image(output/(name+'_hdmi.jpg'),rendered)
                    snapshot_requested=False
                    last_snapshot=t3
                timing=dict(sample,loop_start=loop_start,loop_end=time.monotonic(),wait_ms=wait_ms,
                    display_ack_monotonic=display['display_ack_monotonic'])
                collection.record('display',tuple(timing.get(k) for k in DISPLAY_FIELDS))
        status['status']='STOPPED'
        if latest['error']:raise RuntimeError(latest['error'])
    except BaseException as error:
        status.update(status='FAILED',error=repr(error),traceback=traceback.format_exc())
        print(status['traceback'],flush=True)
    finally:
        stop.set()
        if voice is not None:
            try:
                voice.stop()
                status['voice']=voice.snapshot()
                atomic_json(output/'voice_samples.json',dict(records=list(voice.records),
                    records_dropped=voice.records_dropped,status=voice.snapshot(),
                    timing_boundary='VAD estimated speech end to main-thread UI dispatch; excludes wireless transmission and OBS'))
            except Exception as audio_error:
                status['voice_shutdown_error']=repr(audio_error)
        with shared:shared.notify_all()
        for worker in workers:worker.join(timeout=5)
        if display_path=='pl-camera':
            prediction=latest['prediction']
            status.update(captured_frames=latest['captured'],frame_copy_drops=latest['copy_drops'],
                inference_frames=prediction['result_id'] if prediction else 0,
                latest_ai_compute_ms=prediction['ai_compute_total_ms'] if prediction else None)
        final_status=status['status']
        while owner is not None:
            try:
                if any(w.is_alive() for w in workers):raise RuntimeError('Worker still uses owned hardware')
                camera.halt_video()
                if model.executor is not None:model.executor.wait_idle()
                output_owner=getattr(camera,'hdmi_output',None)
                if output_owner is not None:output_owner.close()
                camera.stop_video()
                if model.executor is not None:model.executor.close()
                handoff=mark_quiescent(camera,model.executor)
                status.update(status=final_status,shutdown_verified=True,handoff=handoff)
                owner.close();owner=None
            except BaseException as error:
                status.update(status='RETAINED_OWNER_RETRY',shutdown_error=repr(error),shutdown_verified=False)
                try:atomic_json(status_path,status)
                except OSError as write_error:print('Status write failed; retaining resources: '+repr(write_error),flush=True)
                print('Retaining DMA resources: '+repr(error),flush=True)
                time.sleep(5)
                for worker in workers:worker.join(timeout=1)
        if status.get('shutdown_verified'):
            try:
                collection.export()
                if maintenance is not None:
                    performance_path=output/'performance_samples.json'
                    performance=json.loads(performance_path.read_text())
                    performance['wb_maintenance']=maintenance.performance_metadata()
                    atomic_json(performance_path,performance)
            except OSError as error:
                collection.storage_failure(output/'performance_samples.json',error)
                status['performance_export']='FAILED'
            else:status['performance_export']='EXPORTED_AFTER_SAFE_STOP'
        if trace is not None:status['gesture_trace']=trace.close(timeout=3)
        status['memory_after']=memory_snapshot()
        atomic_json(status_path,status)
    return 0 if status['status']=='STOPPED' and status.get('shutdown_verified') else 1


if __name__ in ('__main__','__ax7020_locked_main__'):
    p=argparse.ArgumentParser()
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--duration',type=float)
    p.add_argument('--mode',choices=('hybrid',),default='hybrid')
    p.add_argument('--voice-mode',choices=('off','exclusive','gated'),default='exclusive')
    p.add_argument('--display-path',choices=('pl-camera',),default='pl-camera',help='Continuous PL camera; UI is temporarily disabled')
    p.add_argument('--initial-mode',default='home')
    p.add_argument('--diagnostics',action='store_true')
    p.add_argument('--detection-profile',choices=('baseline','palm_balanced'),default='baseline')
    p.add_argument('--detection-hud',action='store_true',help='Show detection count, result age and application mode')
    p.add_argument('--collection-policy',choices=('production','diagnostic'),default='production')
    p.add_argument('--gesture-trace',choices=('off','light','detailed'),default='off')
    p.add_argument('--trace-images-every',type=int,default=0,help='Save same-source image every N results; zero disables images')
    p.add_argument('--glass-quality',choices=('flat','glass','glass_fast'),default='glass_fast')
    a=p.parse_args()
    raise SystemExit(run(a.output.resolve(),a.duration,a.mode,a.initial_mode,a.diagnostics,a.glass_quality,collection_policy=a.collection_policy,gesture_trace=a.gesture_trace,trace_images_every=a.trace_images_every,detection_hud=a.detection_hud,detection_profile=a.detection_profile,voice_mode=a.voice_mode,display_path=a.display_path))
