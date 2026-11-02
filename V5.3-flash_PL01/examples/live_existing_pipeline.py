"""Independent existing-driver adapter; no bitstream or IP address assumptions.

Camera protocol: read_frame() -> a NEW uint8 HWC BGR frame or None at EOF, and
close() which unblocks read_frame(). Borrowed DMA storage must remain stable
until the next read_frame() call. LatestFrameSource copies before that call.
Its sequence/time describe software delivery, not sensor exposure. A driver
must not return the same completed acquisition repeatedly as a new frame.

Synchronous integration (sink consumes/copies AND presents before returning)::

    with create_live_application(camera, existing_runner, config) as live:
        while not live.app.status().get('quit_requested'):
            live.submit_sync(hdmi_sink)
            time.sleep(1 / 30)

For async DMA call submit_async(driver) and poll_presentations() in the display
loop. Neither waits for a fence. Driver owns submitted leases until completion,
including teardown; caller closes/resets display driver AFTER the loop. Capture
and AI keep running even if that display loop is stalled.
"""
from collections import deque
import copy
from pathlib import Path
import time

from run_app import ApplicationPipeline
from spatial_build.runtime import LatestFrameSource
from town_creator.live_runtime import CameraGestureWorker

ROOT = Path(__file__).resolve().parents[1]


class LiveApplication:
    """Own capture/pipeline; caller owns display driver and its DMA leases.

    Camera ownership is transferred here: LatestFrameSource closes it exactly
    once, including EOF or constructor failure. Passing an existing
    LatestFrameSource transfers its close responsibility too. Inference worker
    owns and closes the supplied existing runner.
    """

    def __init__(self, camera_source, existing_runner, config=None, *, root=ROOT,
                 work_path=None, max_pending=3, ui_worker=None, defer_conversion=False):
        if type(max_pending) is not int or not 1 <= max_pending <= 8:
            raise ValueError('max_pending must be an integer in 1..8')
        if not isinstance(camera_source, LatestFrameSource):
            if not callable(getattr(camera_source, 'read_frame', None)) or not callable(getattr(camera_source, 'close', None)):
                raise TypeError('camera_source must implement read_frame() and close()')
        self.config = copy.deepcopy(config or {})
        self.config.setdefault('already_mirrored', False)
        self.config.setdefault('detection_interval_ms', 200)
        self.config.setdefault('voice', {'enabled': False})
        self.config.setdefault('ui_effects', {'glass_mode': 'tint'})
        options = self.config.setdefault('inference', {})
        # Stable defaults, not newly tuned thresholds or input dimensions.
        options.setdefault('confidence', .60)
        options.setdefault('pinch_confidence', .45)
        options.setdefault('iou', .45)
        options.setdefault('restore_policy', 'yolov5')
        voice = self.config['voice']
        from voice_control.session import InferenceGate
        gate = InferenceGate() if voice.get('enabled') and voice.get('policy') == 'gated' else None
        self.capture = (camera_source if isinstance(camera_source, LatestFrameSource)
                        else LatestFrameSource(camera_source,defer_conversion=defer_conversion,
                            on_demand=defer_conversion and getattr(camera_source,'capture_on_demand',False)))
        self.app = None
        self._last_snapshot = None
        self._pending = deque()
        self._last_submitted_ui_revision = -1
        self.ui_layer_submissions = 0
        self._uncertain_leases = []  # Never free storage possibly owned by DMA.
        self.max_pending = max_pending
        self.skipped_no_buffer = self.skipped_pending_limit = 0
        self.failed_presentations = self.cancelled_presentations = 0
        self.last_presentation_error = None
        self._closed = False
        worker = None
        try:
            worker = CameraGestureWorker(runner=existing_runner,
                names=tuple(options.get('names', ('open_palm', 'pinch_point'))),
                interval_ms=self.config['detection_interval_ms'], inference_gate=gate,
                postprocess={key: options[key] for key in
                             ('confidence', 'pinch_confidence', 'iou', 'restore_policy')})
            self.app=ApplicationPipeline.__new__(ApplicationPipeline)
            self._app_initialized=False
            self.app.__init__(self.config, root=root, worker=worker,
                ui_worker=ui_worker, work_path=work_path, inference_gate=gate)
            self._app_initialized=True
            # Required: AI and UI-result polling may not depend on display.
            self.app.bind_capture(self.capture)
        except Exception as exc:
            self._failed_worker=worker
            exc.partial_live_application=self
            try:self.close()
            except Exception as cleanup_error:exc.partial_cleanup_error=repr(cleanup_error)
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _snapshot(self):
        if self._closed or getattr(self,'_closing',False):
            raise RuntimeError('LiveApplication is closed')
        newer = self.capture.snapshot(self._last_snapshot.sequence if self._last_snapshot else 0)
        if newer is not None:
            # LatestFrameSource owns a unique copy. Freeze it before sharing;
            # the next acquisition uses different storage, never this array.
            if newer.image is None:
                raise ValueError('Deferred raw capture uses submit_ui; CPU video composition is disabled')
            newer.image.setflags(write=False)
            self._last_snapshot = newer
        if self.capture.error:
            raise RuntimeError('Camera capture failed: ' + self.capture.error)
        return self._last_snapshot

    @staticmethod
    def _lease_api(driver):
        for name in ('acquire_output_frame', 'submit_output_frame', 'release_output_frame'):
            if not callable(getattr(driver, name, None)):
                raise TypeError('Leased driver must implement ' + name + '()')

    def _compose_lease(self, driver, snapshot, output):
        try:
            return self.app.process_frame(snapshot.image, snapshot.sequence,
                snapshot.captured_at, owned=True, display_buffer=output)
        except Exception:
            driver.release_output_frame(output)
            raise  # No submission attempted: this lease is still ours.

    def submit_sync(self, driver, *, leased=False):
        """A slow synchronous sink blocks only caller, not capture/AI/UI.

        write_frame() / leased submit_output_frame() MUST consume or copy AND
        confirm presentation before returning. A copy into a queue is not
        presentation: queued DMA uses submit_async(). No new source
        yet, or no free output lease, returns None and skips this display.
        """
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        output = None
        if leased:
            self._lease_api(driver)
            output = driver.acquire_output_frame()
            if output is None:
                self.skipped_no_buffer += 1
                return None
            out = self._compose_lease(driver, snapshot, output)
        else:
            if not callable(getattr(driver, 'write_frame', None)):
                raise TypeError('Synchronous sink must implement write_frame()')
            out = self.app.process_frame(snapshot.image, snapshot.sequence,
                snapshot.captured_at, owned=True)
        started = time.perf_counter()
        try:
            if leased:
                driver.submit_output_frame(out, snapshot.sequence, snapshot.captured_at)
            else:
                driver.write_frame(out)
        except Exception:
            if output is not None:
                self._uncertain_leases.append(output)
            raise  # Driver recovery decides ownership; never double-free.
        self.app.record_submission(sequence=snapshot.sequence,
            duration_ms=(time.perf_counter() - started) * 1000)
        return out

    @staticmethod
    def _flag(fence, name, required=False):
        function = getattr(fence, name, None)
        if function is None and not required:
            return False
        if not callable(function):
            raise TypeError('Presentation fence must implement ' + name + '()')
        value = function()
        if type(value) is not bool:
            raise TypeError('Fence ' + name + '() must return bool, not a status enum')
        return value

    def poll_presentations(self):
        """Nonblocking FIFO completion poll; only true presentation ACKs UI.

        Each fence provides presented()->bool; optional cancelled()/failed()
        also return bool. Failed/cancelled completions never unlock that UI
        revision, even if presented() would also return True. 'Presented' must
        mean the corresponding frame became visible, not merely queued.
        """
        completed = 0
        while self._pending:
            fence, revision = self._pending[0]
            try:
                cancelled = self._flag(fence, 'cancelled')
                failed = self._flag(fence, 'failed')
                presented = False if cancelled or failed else self._flag(fence, 'presented', True)
            except Exception as exc:
                self.last_presentation_error = '%s: %s' % (type(exc).__name__, exc)
                failed, cancelled, presented = True, False, False
            if not (presented or failed or cancelled):
                break  # Never ACK a later fence ahead of the FIFO head.
            self._pending.popleft()
            if cancelled:
                self.cancelled_presentations += 1
            elif failed:
                self.failed_presentations += 1
            elif revision is not None:
                self.app.acknowledge_ui(revision)
            completed += 1
        return completed

    def submit_async(self, driver):
        """Return an accepted fence or None; never wait for buffer or fence.

        acquire_output_frame()->private writable uint8 HWC BGR or None;
        submit_output_frame(buffer, source_sequence, delivered_at)->fence;
        release_output_frame(buffer) returns only an UNSUBMITTED lease.
        Successful submit transfers ownership until driver completion/close.
        On a submission exception ownership is uncertain: recover/reset driver
        without blindly freeing or reusing the attempted output buffer.
        """
        self.poll_presentations()
        if len(self._pending) >= self.max_pending:
            self.skipped_pending_limit += 1
            return None
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        self._lease_api(driver)
        output = driver.acquire_output_frame()
        if output is None:
            self.skipped_no_buffer += 1
            return None
        out = self._compose_lease(driver, snapshot, output)
        revision = self.app.last_composed_ui_revision
        started = time.perf_counter()
        try:
            fence = driver.submit_output_frame(out, snapshot.sequence, snapshot.captured_at)
            if not callable(getattr(fence, 'presented', None)):
                raise TypeError('Async submit must return a nonblocking presented() fence')
        except Exception:
            self._uncertain_leases.append(output)
            raise
        self._pending.append((fence, revision))
        self.app.record_submission(sequence=snapshot.sequence,
            duration_ms=(time.perf_counter() - started) * 1000, presented=False)
        return fence

    def submit_ui(self,driver,width=1280,height=720):
        """Submit only immutable UI pixels; camera scanout remains in PL."""
        if self._closed or getattr(self,'_closing',False):raise RuntimeError('LiveApplication is closed')
        if self.capture.error:raise RuntimeError('Camera capture failed: '+self.capture.error)
        if self.app.ui is None:
            from town_creator.live_runtime import UIWorker
            self.app.ui=UIWorker(width,height,root=self.app.root,config=self.app.config,
                work_path=self.app.work_path,app_factory=self.app.app_factory,
                prepare_delay=self.app.prepare_delay)
            if self.app.voice is not None:self.app.voice.attach(self.app.ui)
            self.app.ui.attach_inference(self.app.worker)
        self.poll_presentations()
        packet=self.app.latest_ui_packet()
        if packet is None or packet.revision<=self._last_submitted_ui_revision:return None
        if len(self._pending)>=self.max_pending:
            self.skipped_pending_limit+=1
            return None
        if not callable(getattr(driver,'submit_ui_layer',None)):raise TypeError('UI driver must implement submit_ui_layer()')
        fence=driver.submit_ui_layer(packet)
        if fence is None:
            self.skipped_no_buffer+=1
            return None
        if not callable(getattr(fence,'presented',None)):raise TypeError('UI submit requires a presented() fence')
        self._pending.append((fence,packet.revision))
        self._last_submitted_ui_revision=packet.revision
        self.ui_layer_submissions+=1
        return fence

    def metrics(self):
        return dict(pipeline=self.app.metrics(), capture=self.capture.metrics(),
            pending_presentations=len(self._pending), skipped_no_buffer=self.skipped_no_buffer,
            skipped_pending_limit=self.skipped_pending_limit,
            failed_presentations=self.failed_presentations,
            cancelled_presentations=self.cancelled_presentations,
            ui_layer_submissions=self.ui_layer_submissions,
            uncertain_output_leases=len(self._uncertain_leases),
            last_presentation_error=self.last_presentation_error,
            physical_display_refresh_measured=False)

    def close(self):
        if getattr(self,'_closed',False):
            return True
        self._closing = True
        # Consumers stop before source. Display leases always belong to driver
        # teardown, including pending or failed fences; never free them here.
        try:
            app=getattr(self,'app',None)
            if app is not None and getattr(self,'_app_initialized',True):pipeline_done=app.close()
            else:
                owners=[getattr(app,name,None) for name in ('voice','ui','worker')] if app is not None else []
                worker=getattr(self,'_failed_worker',None)
                if worker is not None and worker not in owners:owners.append(worker)
                outcomes=[owner.close() for owner in owners if owner is not None]
                pipeline_done=all(outcomes)
        finally:
            capture=getattr(self,'capture',None)
            capture_done=True if capture is None else capture.close()
        self._closed=bool(pipeline_done and capture_done)
        return self._closed


def create_live_application(camera_source, existing_runner, config=None, **kwargs):
    """Return fully bound adapter; context manager also closes camera source."""
    return LiveApplication(camera_source, existing_runner, config, **kwargs)


def create_application(existing_runner, camera_source, *, already_mirrored=False,
                       voice_config=None, **kwargs):
    """Source-required replacement; returns adapter with .app and .capture.

    No source-less fallback exists: that could starve AI on HDMI backpressure.
    """
    return create_live_application(camera_source, existing_runner,
        {'already_mirrored': already_mirrored,
         'voice': voice_config or {'enabled': False}}, **kwargs)
