"""Independent live video path: camera -> private output -> prepared uint8 ROI.

UI state/geometry and inference have their own latest-only workers. This module
never builds an interface or calls a model on the display path.
"""
from collections import deque
import argparse
import json
import math
from pathlib import Path
import platform
import time

import cv2
import numpy as np

from gesture_app.core import Detection
from gesture_app.frame_io import OpenCvSource, save_image
from gesture_app.inference import load_factory
from spatial_build.runtime import LatestFrameSource
from combined.serial_app import FourLevelsApp
from combined.voice import NavigationVoice
from town_creator.live_runtime import CameraGestureWorker, UIWorker
from town_creator.layers import DisplayCompositor

ROOT = Path(__file__).resolve().parent
TITLE = 'Spatial Studio'


class MouseWorker:
    """Explicit demonstration input; never impersonates model detections."""
    def offer(self,*args,**kwargs): pass
    def poll(self,*args): return None
    def close(self): return True
    def metrics(self): return dict(mode='explicit mouse demonstration; no model inference',inference_results=0)


def _stats(values):
    if not values:
        return dict(samples=0, median=0., p95=0., maximum=0.)
    return dict(samples=len(values), **{k:float(v) for k,v in zip(
        ('median','p95','maximum'), np.percentile(values,[50,95,100]))})


class ApplicationPipeline:
    """Synchronous display adapter; all heavy preparation is asynchronous.

    ``owned=True`` transfers an immutable camera snapshot reference. Its memory
    must remain valid and untouched while any worker holds it. DMA/reusable
    input buffers should use the default safe copy. Output is private but reused;
    a synchronous sink must finish consuming it before the next call. Pass a
    driver-owned free ``display_buffer`` for asynchronous HDMI/DMA ownership.
    """
    def __init__(self, config, root=ROOT, worker=None, ui_worker=None,
                 work_path=None, app_factory=FourLevelsApp, prepare_delay=0., inference_gate=None):
        self.config, self.root = config, Path(root)
        options=config.get('inference',{})
        voice_config=config.get('voice',{})
        self.inference_gate=inference_gate
        if voice_config.get('enabled') and voice_config.get('policy')=='gated' and self.inference_gate is None:
            from voice_control.session import InferenceGate
            self.inference_gate=InferenceGate()
        self.worker=worker if worker is not None else CameraGestureWorker(
            config=options,root=self.root,interval_ms=config.get('detection_interval_ms',200),
            inference_gate=self.inference_gate,
            postprocess={k:options[k] for k in ('confidence','pinch_confidence','iou','restore_policy') if k in options})
        self.voice=NavigationVoice(voice_config,self.root,gate=self.inference_gate) if voice_config.get('enabled') else None
        if self.voice is not None:self.voice.start()
        self.ui=ui_worker
        if self.voice is not None and self.ui is not None:self.voice.attach(self.ui)
        self.app_factory,self.work_path=app_factory,work_path
        self.prepare_delay=float(prepare_delay)
        self.base=None
        self._output=None
        self.compositor=None
        self.capture_sequence=-1
        self.sequence=0
        self.output_count=0
        self.new_source_frames_received=0
        self.new_source_submissions=0
        self.repeated_source_submissions=0
        self._last_submitted_source=-1
        self._last_composed_source=-1
        self._last_packet=None
        self._first_new_submit_at=self._last_new_submit_at=None
        self._first_composed_at=self._last_composed_at=None
        self._input_copies=0
        self._mirror_ms=deque(maxlen=512)
        self._input_ms=deque(maxlen=512)
        self._compose_ms=deque(maxlen=512)
        self._display_ms=deque(maxlen=512)
        self._path_ms=deque(maxlen=512)
        self._source_age_ms=deque(maxlen=512)
        self._last_captured_at=None
        self._capture_binding=None
        self.last_error=None
        self.started_at=time.perf_counter()

    def _accept(self,bgr,sequence,captured_at,owned):
        if not math.isfinite(float(captured_at)):raise ValueError('captured_at must be finite')
        if (not isinstance(bgr,np.ndarray) or bgr.dtype!=np.uint8 or bgr.ndim!=3
                or bgr.shape[2]!=3 or min(bgr.shape[:2])<=0):
            raise ValueError('Input must be nonempty uint8[H,W,3] BGR')
        if type(sequence) is not int or sequence<0:
            raise ValueError('Capture sequence must be a monotonic nonnegative integer')
        if self.base is not None and sequence<=self.capture_sequence:
            return False
        if owned:
            if bgr.flags.writeable or not bgr.flags.c_contiguous:
                raise ValueError('owned input must be a read-only contiguous snapshot with transferred lifetime')
            source=bgr
        else:
            source=bgr.copy(order='C');source.setflags(write=False);self._input_copies+=1
        h,w=source.shape[:2]
        if self.base is not None and source.shape!=self.base.shape:
            raise ValueError('Video resolution changed; restart to preserve display/hit coordinates')
        if self.compositor is None:
            self.compositor=DisplayCompositor(w,h,effects=self.config.get('ui_effects'))
        if self.ui is None:
            self.ui=UIWorker(w,h,root=self.root,config=self.config,work_path=self.work_path,
                             app_factory=self.app_factory,prepare_delay=self.prepare_delay)
            if self.voice is not None:self.voice.attach(self.ui)
            if self._capture_binding is not None:self.ui.attach_inference(self.worker)
        self.base,self.capture_sequence=source,sequence
        self._last_captured_at=float(captured_at)
        self.new_source_frames_received+=1
        if self._capture_binding is not None:
            pass  # AI pulls the newest capture independently of HDMI/display.
        elif hasattr(self.worker,'offer_camera'):
            self.worker.offer_camera(source,captured_at=captured_at,source_sequence=sequence,
                already_mirrored=self.config.get('already_mirrored',False),owned=True)
        elif not isinstance(self.worker,MouseWorker):
            raise TypeError('Worker must implement offer_camera; raw video may not enter a mirrored-input worker')
        return True

    def bind_capture(self,capture):
        """Use a LatestFrameSource directly so display backpressure cannot starve AI.

        This source guarantees a private stable snapshot per acquisition. A
        borrowed DMA source must first be adapted/copied by LatestFrameSource.
        """
        if not hasattr(self.worker,'attach_camera_source'):
            if isinstance(self.worker,MouseWorker):return False
            raise TypeError('Camera worker lacks a latest-source pull adapter')
        self.worker.attach_camera_source(capture,already_mirrored=self.config.get('already_mirrored',False))
        self._capture_binding=capture
        if self.ui is not None:self.ui.attach_inference(self.worker)
        return True

    def process_frame(self,bgr=None,capture_sequence=None,captured_at=None,now=None,
                      owned=False,display_buffer=None):
        before=time.perf_counter();now=before if now is None else float(now)
        if not math.isfinite(now):raise ValueError('now must be finite')
        if bgr is not None:
            capture_sequence=self.capture_sequence+1 if capture_sequence is None else capture_sequence
            stamp=now if captured_at is None else float(captured_at)
            self._accept(bgr,capture_sequence,stamp,owned)
        self._input_ms.append((time.perf_counter()-before)*1000)
        if self.base is None:return None
        result=self.worker.poll(self.sequence)
        if result is not None:
            self.sequence=result.sequence
            if result.error:self.last_error=result.error
            if self._capture_binding is None:self.ui.offer_result(result)
        if display_buffer is None and self._output is None:self._output=np.empty_like(self.base)
        output=self._output if display_buffer is None else display_buffer
        if (not isinstance(output,np.ndarray) or output.shape!=self.base.shape or output.dtype!=np.uint8
                or not output.flags.c_contiguous or not output.flags.writeable or np.shares_memory(output,self.base)):
            raise ValueError('Display buffer must be a private writable contiguous BGR frame of the capture size')
        step=time.perf_counter()
        if self.config.get('already_mirrored',False):np.copyto(output,self.base)
        else:cv2.flip(self.base,1,dst=output)
        self._mirror_ms.append((time.perf_counter()-step)*1000)
        packet=self.ui.latest()
        step=time.perf_counter()
        self.compositor.compose_into(output,packet)
        self._compose_ms.append((time.perf_counter()-step)*1000)
        self._last_packet=packet
        self.output_count+=1
        self._last_composed_source=self.capture_sequence
        if self._first_composed_at is None:self._first_composed_at=now
        self._last_composed_at=now
        self._source_age_ms.append(max(0.,(now-self._last_captured_at)*1000))
        self._path_ms.append((time.perf_counter()-before)*1000)
        return output

    def record_submission(self,sequence=None,duration_ms=0.,submitted_at=None,presented=True):
        """Call only AFTER a real sink accepts the frame, including headless tests.

        Distinct source sequence count is separate from repeated refreshes.
        Headless acceptance never claims HDMI electrical scanout.
        """
        sequence=self._last_composed_source if sequence is None else int(sequence)
        now=time.perf_counter() if submitted_at is None else float(submitted_at)
        self._display_ms.append(float(duration_ms))
        if sequence>self._last_submitted_source:
            self.new_source_submissions+=1;self._last_submitted_source=sequence
            if self._first_new_submit_at is None:self._first_new_submit_at=now
            self._last_new_submit_at=now
        else:self.repeated_source_submissions+=1
        if presented and self._last_packet is not None:self.ui.acknowledge(self._last_packet.revision)

    def acknowledge_ui(self,revision):
        """Async video drivers call this after the corresponding UI is presented."""
        if self.ui is not None:self.ui.acknowledge(int(revision))

    def latest_ui_packet(self):
        """Camera-free immutable ROI packet for a future hardware compositor."""
        return self.ui.latest() if self.ui is not None else None

    @property
    def last_composed_ui_revision(self):
        return self._last_packet.revision if self._last_packet is not None else None

    @property
    def output_source_sequence(self):
        return self._last_composed_source

    @property
    def output_captured_at(self):
        return self._last_captured_at

    def status(self):
        return self.ui.status() if self.ui is not None else dict(ready=False,quit_requested=False,error=None)

    def metrics(self):
        def rate(count,first,last):return (count-1)/(last-first) if count>1 and last>first else 0.
        return dict(elapsed_seconds=time.perf_counter()-self.started_at,
            composed_output_frames=self.output_count,
            composition_hz=rate(self.output_count,self._first_composed_at,self._last_composed_at),
            new_source_frames_received=self.new_source_frames_received,
            new_source_submissions=self.new_source_submissions,
            new_source_submission_hz=rate(self.new_source_submissions,self._first_new_submit_at,self._last_new_submit_at),
            repeated_source_submissions=self.repeated_source_submissions,
            last_source_sequence=self.capture_sequence,boundary_input_copies=self._input_copies,
            inference=self.worker.metrics(),ui=self.ui.metrics() if self.ui is not None else {},
            status=self.status(),compositor=self.compositor.metrics() if self.compositor is not None else {},
            timing_ms=dict(input_exchange=_stats(self._input_ms),mirror_or_copy=_stats(self._mirror_ms),
                composition=_stats(self._compose_ms),display_submission=_stats(self._display_ms),
                whole_display_path=_stats(self._path_ms),source_delivery_age=_stats(self._source_age_ms)),
            last_inference_error=self.last_error,voice_enabled=self.voice is not None,
            voice=self.voice.metrics() if self.voice is not None else {'enabled':False},
            additional_ai_models=0,
            physical_display_refresh_measured=False,
            model_ui_ownership='immutable clean raw camera -> AI mirror in worker; private mirrored display -> ROI only')

    def close(self):
        voice_done=True if self.voice is None else self.voice.close()
        ui_done=True if self.ui is None else self.ui.close()
        ai_done=self.worker.close()
        return bool(voice_done and ui_done and ai_done)


def main(argv=None):
    p=argparse.ArgumentParser(description='Three projection levels and a free town studio')
    p.add_argument('--config',default='configs/pc.json');p.add_argument('--source')
    p.add_argument('--no-voice',action='store_true',help='Explicit silent demo or performance comparison')
    p.add_argument('--seconds',type=float,default=0.);p.add_argument('--headless',action='store_true')
    p.add_argument('--mouse-only',action='store_true');p.add_argument('--demo',action='store_true')
    p.add_argument('--work');p.add_argument('--load',action='store_true');p.add_argument('--report');p.add_argument('--screenshot')
    p.add_argument('--board-measurement',action='store_true')
    p.add_argument('--ui-delay-ms',type=float,default=0.,help='Explicit stress test only; delays UI preparation')
    p.add_argument('--glass-mode',choices=('tint','board_fast','local_blur'),help='Explicit visual-effect configuration')
    args=p.parse_args(argv)
    path=Path(args.config);path=path if path.is_absolute() else ROOT/path
    config=json.loads(path.read_text(encoding='utf-8'))
    if args.no_voice:config.setdefault('voice',{})['enabled']=False
    if args.source is not None:config.setdefault('video',{})['source']=args.source;config['video_backend']='opencv'
    if args.glass_mode is not None:config.setdefault('ui_effects',{})['glass_mode']=args.glass_mode
    work=Path(args.work) if args.work else ROOT/'works/latest.json'
    if not work.is_absolute():work=ROOT/work
    pipeline=capture=bridge=None;mouse=dict(down=False,point=(0,0),last=-1.)
    def mouse_event(event,x,y,flags,param):
        mouse['point']=x,y
        if event==cv2.EVENT_LBUTTONDOWN:mouse['down']=True
        elif event==cv2.EVENT_LBUTTONUP:mouse['down']=False
    try:
        if not args.demo:
            if config.get('video_backend','opencv')=='plugin':
                bridge=load_factory(config['video_factory'])(config.get('video_options',{}));source=bridge
            else:source=OpenCvSource(config.get('video',{}),ROOT)
            capture=LatestFrameSource(source)
        pipeline=ApplicationPipeline(config,worker=MouseWorker() if args.demo or args.mouse_only else None,
            work_path=work,prepare_delay=args.ui_delay_ms/1000.)
        if capture is not None and not args.mouse_only:pipeline.bind_capture(capture)
        if args.demo:
            w,h=int(config.get('video',{}).get('width',1280)),int(config.get('video',{}).get('height',720))
            yy,xx=np.mgrid[:h,:w];demo=np.stack((95+xx*35/w,92+yy*40/h,89+xx*20/w),-1).astype(np.uint8)
        if not args.headless:
            cv2.namedWindow(TITLE,cv2.WINDOW_AUTOSIZE)
            if args.demo or args.mouse_only:cv2.setMouseCallback(TITLE,mouse_event)
        start=deadline=time.perf_counter();sequence=outputs=0;output=None;loaded=False;showed=False;reported_error=None;voice_state=None
        acquire_times=deque(maxlen=512);no_free_buffers=0;last_snapshot=None
        output_mode=config.get('video_output_mode','synchronous')
        if output_mode not in ('synchronous','synchronous_lease'):
            raise ValueError('Use the explicit async-driver example for asynchronous presentation fences')
        while True:
            now=time.perf_counter()
            if args.seconds and now-start>=args.seconds:break
            if args.demo:
                frame=demo.copy();cv2.circle(frame,(int((now-start)*40)%w,int(h*.9)),24,(170,180,170),-1)
                frame.setflags(write=False);sequence+=1;captured=now
            else:
                snap=capture.snapshot(sequence)
                if snap:sequence=snap.sequence;last_snapshot=snap;snap.image.setflags(write=False)
                # Hold only the latest stable snapshot. If HDMI had no free
                # buffer last turn, the next turn still uses this newest frame.
                frame=last_snapshot.image if last_snapshot is not None else None
                captured=last_snapshot.captured_at if last_snapshot is not None else None
                if capture.error:raise RuntimeError(capture.error)
                if capture.finished and snap is None:
                    if pipeline.base is None:raise RuntimeError('No camera frame received')
                    break
            display_buffer=None
            if bridge is not None and config.get('write_video',False) and output_mode=='synchronous_lease':
                wait_started=time.perf_counter();display_buffer=bridge.acquire_output_frame()
                acquire_times.append((time.perf_counter()-wait_started)*1000)
                if display_buffer is None:
                    no_free_buffers+=1
                    if not args.headless and cv2.waitKey(1)&255 in (27,ord('q')):break
                    time.sleep(.002)
                    continue
            output=pipeline.process_frame(frame,sequence,captured,now,owned=frame is not None,display_buffer=display_buffer)
            if pipeline.ui is not None:
                if args.load and not loaded:
                    pipeline.ui.command('load',work);loaded=True
                if (args.demo or args.mouse_only) and now-mouse['last']>=.2:
                    x,y=mouse['point'];dets=[Detection('pinch_point',1,(x-2,y-2,x+2,y+2))] if mouse['down'] else []
                    pipeline.ui.offer_manual(dets,now);mouse['last']=now
                state=pipeline.ui.control_status()
                if state.get('quit_requested'):break
                error=state.get('error') or state.get('command_error') or pipeline.last_error
                if error and error!=reported_error:print('Worker: '+str(error),flush=True);reported_error=error
                if pipeline.voice is not None:
                    voice_status=pipeline.voice.status()
                    current=voice_status.get('error') or voice_status.get('status')
                    if current and current!=voice_state:print('Voice: '+str(current),flush=True);voice_state=current
            if output is not None:
                submitted=time.perf_counter()
                if bridge is not None and config.get('write_video',False):
                    if display_buffer is not None:bridge.submit_output_frame(output,pipeline.output_source_sequence,pipeline.output_captured_at)
                    else:bridge.write_frame(output)
                if not args.headless:cv2.imshow(TITLE,output);showed=True
                pipeline.record_submission(duration_ms=(time.perf_counter()-submitted)*1000);outputs+=1
            key=cv2.waitKey(1)&255 if not args.headless else 255
            if key in (27,ord('q')):break
            if key==ord('d') and pipeline.ui is not None:pipeline.ui.command('debug')
            if showed and not args.headless and cv2.getWindowProperty(TITLE,cv2.WND_PROP_VISIBLE)<1:break
            deadline+=1/config.get('render_fps',30);delay=deadline-time.perf_counter()
            if delay>0:time.sleep(min(delay,.04))
            else:deadline=time.perf_counter()
        report=pipeline.metrics();report.update(
            measured_on='board (operator declared)' if args.board_measurement else 'computer; not FPGA board',
            capture=capture.metrics() if capture else {'mode':'synthetic moving demonstration'},
            output_submissions=outputs,submission_sink='headless acceptance' if args.headless and bridge is None else 'driver/GUI submission',
            host=dict(system=platform.system(),machine=platform.machine(),python=platform.python_version(),opencv=cv2.__version__))
        report['timing_ms']['display_acquire']=_stats(acquire_times)
        report['output_buffer_mode']=output_mode
        report['no_free_output_buffers']=no_free_buffers
        if args.screenshot and output is not None:save_image(args.screenshot,output)
        if args.report:
            out=Path(args.report);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False,indent=2));return 0
    finally:
        if pipeline:pipeline.close()
        if capture:capture.close()
        if not args.headless:cv2.destroyAllWindows()


if __name__=='__main__':raise SystemExit(main())
