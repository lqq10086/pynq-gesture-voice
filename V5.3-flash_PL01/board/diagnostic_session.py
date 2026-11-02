"""Explicit pre-production owner for cache/VIEW_COPY/new-model numeric probes."""
import json
import os
from pathlib import Path
import platform
import sys
import threading

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'backend_v51/runtime'))
_RETAINED_DIAGNOSTICS={}


def retained_diagnostic_sessions():
    """Expose partial startup owners for the root lifecycle controller to retry."""
    return tuple(_RETAINED_DIAGNOSTICS.values())


class DiagnosticSession:
    scope='PL01_OWNED_DIAGNOSTICS_ONLY'
    video_scope='REALTIME_CAMERA_REQUIRED'
    def __init__(self,root=ROOT):
        import board_identity
        self.root=Path(root);self.admission=board_identity.verify_diagnostic(self.root)
        self.camera=self.owner=self.core_guard=self.pl=self.runner=None;self.retained_probe_buffers=[];self.closed=False;self.closing=False;self.worker_stopped=False
        self.executed_on_target=os.name!='nt' and platform.machine().lower().startswith(('arm','aarch'))
        if not self.executed_on_target:raise RuntimeError('owned diagnostic session is ARM target-only; host tests use explicit fake sessions')
        from owner_guard import acquire_owner,_process_start_ticks
        from camera_control import CameraControlSession
        from board.owned_hardware import ensure_core_guard
        self.boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip();self.owner_start_ticks=_process_start_ticks();self.camera=CameraControlSession()
        self.static=self.admission['camera_overlay'];self.camera.pl01_static_identity=self.static;self.camera.pl01_owner_check=self.owner_check
        self._artifact_cache={}
        self.camera.pl01_shutdown_owner_check=self.shutdown_owner_check
        _RETAINED_DIAGNOSTICS[id(self)]=self
        self.owner=acquire_owner();self.load_result=self._load_overlay();self.core_guard=ensure_core_guard(self.camera);self.core_guard.check_idle()

    def _load_overlay(self):return self.camera.load_overlay(self.static['bitfile_name'])

    def owner_check(self):
        from owner_guard import verify_owner_context
        return not self.closing and verify_owner_context(self.owner,self.boot_id,self.owner_start_ticks) and self._same_artifacts()

    def _same_artifacts(self):
        if self.camera is None or self.camera.overlay is None:return False
        loaded=getattr(self.camera.overlay,'is_loaded',False)
        if not (loaded() if callable(loaded) else loaded) or Path(self.camera.overlay.bitfile_name).resolve()!=Path(self.static['bitfile_name']).resolve():return False
        from sensor_wb import _artifact_digest
        for path,key in [('bitfile_name','bitstream_sha256'),('hwh_path','hwh_sha256')]:
            candidate=Path(self.static[path])
            initial=str(candidate) not in self._artifact_cache
            if _artifact_digest(candidate,self._artifact_cache,initial=initial)!=self.static[key]:return False
        return True

    def shutdown_owner_check(self):
        from owner_guard import verify_owner_context
        current=threading.get_ident()
        stopped=(self.closing and self.worker_stopped and (self.runner is None or self.runner.closed and
            (not self.runner.worker_lock.locked() or getattr(self.runner,'release_thread_id',None)==current))
            and (self.pl is None or self.pl.closed and (not self.pl.lock.locked() or getattr(self.pl,'release_thread_id',None)==current)))
        return stopped and verify_owner_context(self.owner,self.boot_id,self.owner_start_ticks,shutdown=True,closing_confirmed=True) and self._same_artifacts()

    def make_runner(self):
        if self.closing or self.closed:raise RuntimeError('closing diagnostic owner cannot start inference')
        if self.pl is not None or self.runner is not None:raise RuntimeError('existing/partial PL runner retained; close the owned session before replacement')
        from pynq import allocate
        from board.cache_qualification import load_verified_sync
        from board.pl_adapter import PLRunnerAdapter
        sys.path.insert(0,str(self.root/'board'));from pl_runner import PLRunner
        sync,proof=load_verified_sync(self.root,self.static);library=self.root/'board/build_pl_batch/armv7/libpl_batch.so'
        self.pl=PLRunner.__new__(PLRunner)
        self.pl.__init__(self.camera.overlay,self.core_guard.mmio,allocate,self.owner_check,self.core_guard.runner_proof(self.root,library),
            cache_sync=sync,cache_proof=proof,library=library,shutdown_owner_check=self.shutdown_owner_check)
        self.runner=PLRunnerAdapter.__new__(PLRunnerAdapter);self.runner.__init__(self.pl,root=self.root,core_guard=self.core_guard);return self.runner

    def close(self):
        if self.closed:return True
        self.closing=True
        if self.runner is not None and not self.runner.close():return False
        if self.runner is None and self.pl is not None:
            self.pl.close()
            if self.pl.lock.locked():return False
        self.worker_stopped=True
        if self.owner is None:return False
        self.camera.halt_video()
        from board.owned_hardware import ensure_core_guard
        self.core_guard=ensure_core_guard(self.camera,shutdown=True);self.core_guard.check_idle(shutdown=True)
        if self.runner is not None:
            if not self.runner.release_hardware(owner_confirmed=True):return False
        elif self.pl is not None:
            self.pl.close()
            if not self.pl.release(owner_confirmed=True):return False
        for buffer in self.retained_probe_buffers:buffer.freebuffer()
        self.retained_probe_buffers.clear();self.camera.stop_video()
        from owner_guard import mark_quiescent
        self.handoff=mark_quiescent(self.camera,self.core_guard);self.owner.close();self.closed=True;_RETAINED_DIAGNOSTICS.pop(id(self),None);return True


class NNOnlyDiagnosticSession(DiagnosticSession):
    """Explicit static NN/cache owner: camera absence never becomes video PASS."""
    video_scope='NN_ONLY_NO_SENSOR_OR_VIDEO_ACCEPTANCE'
    def _load_overlay(self):
        from board.nn_only_overlay import load_nn_only_overlay
        return load_nn_only_overlay(self.camera,self.static['bitfile_name'])
