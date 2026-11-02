"""Continuous camera/UI owner with the new Fastest PL01 inference core."""
from collections import deque
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

import numpy as np

ROOT=Path(__file__).resolve().parent
BACKEND=ROOT/'backend_v51'
RUNTIME=BACKEND/'runtime'
sys.path.insert(0,str(RUNTIME))
os.environ.setdefault('XILINX_XRT','/usr')
# One bare module name is essential: owner_guard retains the live lock globally.
from owner_guard import acquire_owner,mark_quiescent,verify_owner_context,_process_start_ticks

_system=None
_init_lock=threading.Lock()






class HardwareSystem:
    # PL video scanout is independent; inference needs one fresh snapshot
    # per result, rather than copying every 30fps camera frame into PS memory.
    capture_on_demand=True

    def __init__(self,output_dir=None,identity_check=None,apply_wb=True):
        # All partial ownership remains reachable if any subsequent step fails.
        self.runner=self.camera=self.owner=self.ui_plane=self.detection_overlay=self.pl=self.core_guard=None
        self.stop=threading.Event()
        self.closed=False
        self.closing=False;self.worker_stopped=False
        self.startup_complete=False
        self.shutdown_stage='INITIALIZING'
        self.output_dir=Path(output_dir or ROOT/'reports'/'board_backend').resolve()
        self.output_dir.mkdir(parents=True,exist_ok=True)
        self.frames=self.rejected=self.converted_frames=0
        self.capture_samples=deque(maxlen=512)
        self.conversion_samples=deque(maxlen=512)
        self.last_capture=None
        self.wb=None
        self.overlay_clear_error=None
        self.neural_core_presence=None
        self._frontend_check=identity_check
        if self._frontend_check is None:
            from board_identity import verify
            self._frontend_check=lambda:verify(ROOT)
        self.frontend_identity=self._frontend_check()
        self.boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        self.owner_start_ticks=_process_start_ticks()
        self.bit=Path(self.frontend_identity['camera_overlay']['bitfile_name'])
        self.hwh=self.bit.with_suffix('.hwh')
        self._artifact_cache={}
        self.artifacts=self._artifact_identities(initial=True)
        from camera_control import CameraControlSession
        from hardware_idle import require_pynq_device
        from camera_qualification import verify_camera_overlay
        from frame_reader import CompletedFrameReader
        from region_executor import check_global_addresses
        from ui_plane_output import UIPlaneOutput
        require_pynq_device()
        self.camera_identity=verify_camera_overlay(ROOT)
        self.camera=CameraControlSession()
        self.owner=acquire_owner()
        self.camera.pl01_static_identity=self.camera_identity
        self.camera.pl01_owner_check=self._pl_owner_check
        self.camera.pl01_shutdown_owner_check=self._pl_shutdown_owner_check
        # Keep the qualified requested layout reachable if sensor-ready fails
        # after a successful download; guard still checks actual loaded bytes.
        self.camera.bit_path=self.bit
        self.camera.load_overlay(self.bit)
        from board.owned_hardware import ensure_core_guard
        self.core_guard=ensure_core_guard(self.camera)
        self.neural_core_presence=dict(state='PRESENT',startup_idle=self.core_guard.check_idle(),live_ap_ctrl_polled=False)
        from board.cache_qualification import load_verified_sync
        cache_sync,cache_proof=load_verified_sync(ROOT,self.core_guard.identity)
        sys.path.insert(0,str(ROOT/'board'))
        from pl_runner import PLRunner
        from board.pl_adapter import PLRunnerAdapter
        from pynq import allocate
        library=ROOT/'board/build_pl_batch/armv7/libpl_batch.so'
        self.pl=PLRunner.__new__(PLRunner)
        self.pl.__init__(self.camera.overlay,self.core_guard.mmio,allocate,self._pl_owner_check,
            self.core_guard.runner_proof(ROOT,library),cache_sync=cache_sync,cache_proof=cache_proof,library=library,shutdown_owner_check=self._pl_shutdown_owner_check)
        self.runner=PLRunnerAdapter.__new__(PLRunnerAdapter)
        self.runner.__init__(self.pl,root=ROOT,core_guard=self.core_guard)
        self.ui_plane=UIPlaneOutput.__new__(UIPlaneOutput)
        self.ui_plane.__init__(self.camera)
        self.camera.pl01_ui_mmio=self.ui_plane.regs
        from dual_overlay import OverlayPresenter
        self.detection_overlay=OverlayPresenter(self.camera)
        check_global_addresses([self.camera,self.ui_plane]+list(self.pl.buffers.values()))
        self.reader=CompletedFrameReader(self.camera)
        if apply_wb:self.wb=self.restore_wb()
        self.startup_complete=True
        self.shutdown_stage='RUNNING'

    def _pl_owner_check(self):
        return not self.closing and verify_owner_context(self.owner,self.boot_id,self.owner_start_ticks) and self._same_loaded_artifacts()

    def _same_loaded_artifacts(self):
        if self.camera is None or self.camera.overlay is None:return False
        loaded=getattr(self.camera.overlay,'is_loaded',False)
        return bool(loaded() if callable(loaded) else loaded) and Path(self.camera.overlay.bitfile_name).resolve()==self.bit.resolve() and self._artifact_identities()==self.artifacts

    def _pl_shutdown_owner_check(self):
        current=threading.get_ident()
        stopped=(self.closing and self.worker_stopped and (self.runner is None or self.runner.closed and
            (not self.runner.worker_lock.locked() or getattr(self.runner,'release_thread_id',None)==current))
            and (self.pl is None or self.pl.closed and (not self.pl.lock.locked() or getattr(self.pl,'release_thread_id',None)==current)))
        return stopped and verify_owner_context(self.owner,self.boot_id,self.owner_start_ticks,shutdown=True,closing_confirmed=True) and self._same_loaded_artifacts()

    def current_owner_identity(self):
        """Check this frontend, live owner and loaded artifacts before each WB operation."""
        if self._frontend_check()!=self.frontend_identity:raise RuntimeError('Application identity changed')
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        handoff=json.loads(Path('/run/ax7020-v300-handoff.json').read_text())
        if (boot!=self.boot_id or handoff.get('boot_id')!=boot or handoff.get('state')!='ACTIVE'
                or handoff.get('owner_pid')!=os.getpid() or self.owner is None or self.owner.closed):
            raise RuntimeError('Exclusive current-boot owner identity changed')
        camera=self.camera
        if not camera.video_running or Path(camera.bit_path).resolve()!=self.bit.resolve():
            raise RuntimeError('Camera is not running this candidate bitstream')
        loaded=getattr(camera.overlay,'is_loaded',False)
        if not (loaded() if callable(loaded) else loaded):raise RuntimeError('Known overlay is no longer loaded')
        if Path(camera.overlay.bitfile_name).resolve()!=self.bit.resolve():raise RuntimeError('Loaded overlay path differs')
        if self._artifact_identities()!=self.artifacts:raise RuntimeError('Loaded artifact identity changed')
        return dict(root=str(ROOT),output=str(self.output_dir),owner_pid=os.getpid(),boot_id=boot,
            application=self.frontend_identity,
            bitstream=dict(path=str(self.bit),sha256=self.artifacts[str(self.bit)]),
            hwh=dict(path=str(self.hwh),sha256=self.artifacts[str(self.hwh)]))

    def _artifact_identities(self,initial=False):
        from sensor_wb import _artifact_digest
        return {str(path):_artifact_digest(path,self._artifact_cache,initial=initial) for path in (self.bit,self.hwh)}

    def restore_wb(self):
        """Apply the accepted rebound profile before capture/inference/UI workers start."""
        import sensor_wb
        from wb_profile import _verify_final,CONTROL_MASKS
        from camera_qualification import load_wb_profile
        profile,parameters=load_wb_profile(ROOT)
        identity=self.current_owner_identity()
        for key,field in (('bitstream','bitstream_sha256'),('hwh','hwh_sha256')):
            if identity[key]['sha256']!=parameters[field]:raise RuntimeError('Accepted WB hardware differs')
        if parameters['mode']!='manual' or parameters['gains']!=[1460,1024,1796]:
            raise RuntimeError('Expected the preserved accepted manual WB gains')
        folder=self.output_dir/'sensor_wb_startup'/(str(time.time_ns())+'_'+uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        client=sensor_wb.SensorWbClient(self.camera.control_mmio,identity=identity,
            identity_check=self.current_owner_identity,allow_write=True)
        first=None
        record=dict(schema='accepted-wb-startup-v1',state='PREPARED',identity=identity,profile=profile)
        journal=folder/'apply.json'
        with client:
            record['pre']=client.snapshot()
            sensor_wb._journal(journal,record,initial=True)
            try:
                first=folder/'01_set_gains.json'
                client.set_gains(*parameters['gains'],first)
                for index,key in enumerate(('5001','5196','3406'),2):
                    client.rmw8(int(key,16),CONTROL_MASKS[key],parameters['controls'][key],folder/('%02d_rmw_%s.json'%(index,key)))
                record['post']=client.snapshot()
                _verify_final(record['post'],parameters)
                record['state']='VERIFIED_PENDING_CLIENT_CLOSE'
                sensor_wb._journal(journal,record)
            except Exception as error:
                record.update(state='FAILED',error=repr(error))
                if first is not None and first.exists():
                    try:
                        client.restore(first,folder/'restore_original.json')
                        record['restoration']='RESTORED'
                    except Exception as restore_error:record['restoration_error']=repr(restore_error)
                sensor_wb._journal(journal,record)
                raise
        record['state']='COMPLETE'
        sensor_wb._journal(journal,record)
        return dict(status='APPLIED',journal=str(journal),post=record['post'])

    def read_frame_deferred(self):
        from spatial_build.runtime import DeferredFrame
        started=time.monotonic()
        while not self.stop.is_set():
            try:packet=self.reader.read(timeout=.5)
            except (RuntimeError,TimeoutError) as error:
                if isinstance(error,RuntimeError) and 'stable VDMA buffer window' not in str(error):raise
                self.rejected+=1
                if time.monotonic()-started>5:raise
                continue
            raw=packet.pop('raw565')
            raw.setflags(write=False)
            deferred=DeferredFrame(raw,(720,1280,3),self._convert_raw,
                packet['source_frame_id'],packet['source_frame_complete_monotonic'])
            self.frames+=1
            self.last_capture=packet
            self.capture_samples.append(packet)
            return deferred
        return None

    def _convert_raw(self,raw):
        from frame_reader import rgb565_to_bgr
        started=time.monotonic()
        image=rgb565_to_bgr(raw)
        self.conversion_samples.append((time.monotonic()-started)*1000)
        self.converted_frames+=1
        return image

    def read_frame(self):
        packet=self.read_frame_deferred()
        return packet.to_bgr() if packet is not None else None

    def submit_ui_layer(self,prepared):return self.ui_plane.submit_ui_layer(prepared)

    def poll_detection_overlay(self,worker):return self.detection_overlay.poll_worker(worker)

    def close(self):
        self.stop.set()
        return True

    def metrics(self):
        from spatial_build.runtime import _stats
        return dict(captured_valid=self.frames,rejected_unstable=self.rejected,
            latest_capture=self.last_capture,converted_frames=self.converted_frames,
            capture_timing_ms={key:_stats([x[key] for x in list(self.capture_samples)]) for key in ('copy_ms','wait_transition_ms')},
            conversion_ms=_stats(list(self.conversion_samples)),runner=self.runner.identity() if self.runner else None,
            runner_last_timings=self.runner.last_timings if self.runner else None,
            ui_plane=self.ui_plane.metrics() if self.ui_plane else None,
            dual_overlay=self.detection_overlay.metrics() if self.detection_overlay else None,
            neural_pl_core=self.neural_core_presence,
            pl_neural_network_invocations=self.runner.batches*96 if self.runner else 0,
            overlay_clear_error=self.overlay_clear_error,
            wb=self.wb,startup_complete=self.startup_complete,shutdown_stage=self.shutdown_stage,
            presentation='Continuous PL camera; independent UI last-active-pixel fences; OBS acceptance separate')

    def shutdown(self):
        if self.closed:return True
        self.stop.set()
        self.closing=True
        if self.runner is not None and not self.runner.close():raise RuntimeError('Application inference worker still active; owner and all buffers retained')
        if self.runner is None and self.pl is not None:
            self.pl.close()
            if self.pl.lock.locked():raise RuntimeError('Partial PL batch still active; retain owner and buffers')
        self.worker_stopped=True
        if self.owner is not None:
            self.shutdown_stage='BBOX_CLEAR'
            if self.detection_overlay is not None:
                try:self.detection_overlay.clear()
                except Exception as error:self.overlay_clear_error=repr(error)
            # Camera must keep draining while the UI DMA halt is confirmed.
            self.shutdown_stage='UI_HALT'
            if self.ui_plane is not None and self.ui_plane.regs is not None:
                from ui_plane_output import CONTROL,STATUS
                regs=self.ui_plane.regs;regs.write(CONTROL,int(regs.read(CONTROL))&~1)
                deadline=time.monotonic()+self.ui_plane.timeout_s
                while not int(regs.read(STATUS))&1:
                    if time.monotonic()>=deadline:raise TimeoutError('UI DMA halt unconfirmed; all buffers retained')
                    time.sleep(.001)
            self.shutdown_stage='CAMERA_HALT'
            self.camera.halt_video()
            from board.owned_hardware import ensure_core_guard
            self.core_guard=ensure_core_guard(self.camera,shutdown=True)
            self.neural_core_presence=self.core_guard.check_idle(shutdown=True)
            self.shutdown_stage='FASTEST_IDLE_PHASE0_RELEASE'
            if self.runner is not None:
                if not self.runner.release_hardware(owner_confirmed=True):raise RuntimeError('Fastest DMA buffers retained')
            elif self.pl is not None:
                self.pl.close()
                if not self.pl.release(owner_confirmed=True):raise RuntimeError('Partial Fastest DMA buffers retained')
            if self.ui_plane is not None:self.ui_plane.close()
            self.camera.stop_video()
            self.shutdown_stage='QUIESCENCE'
            self.handoff=mark_quiescent(self.camera,self.core_guard)
            self.owner.close()
        elif self.pl is not None:raise RuntimeError('PL buffers exist without an owner; retain all resources for explicit recovery')
        self.shutdown_stage='CLOSED'
        self.closed=True
        return True


def get_system(**options):
    global _system
    with _init_lock:
        if _system is None:
            _system=HardwareSystem.__new__(HardwareSystem)
            _system.__init__(**options)
        if _system.closed:raise RuntimeError('Hardware owner closed; restart this process')
        if not _system.startup_complete:raise RuntimeError('Hardware startup failed; safely close retained partial owner')
        return _system


def create_video(options):
    if options.get('width',1280)!=1280 or options.get('height',720)!=720:raise ValueError('Qualified video is 1280x720')
    return get_system(**{key:options[key] for key in ('output_dir','identity_check','apply_wb') if key in options})


def create_runner(options):
    if options.get('input_shape',[1,3,384,384])!=[1,3,384,384] or options.get('output_shape',[1,2160,7])!=[1,2160,7]:
        raise ValueError('Wrong fixed two-class model contract')
    return get_system().runner
