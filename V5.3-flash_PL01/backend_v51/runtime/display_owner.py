"""One spawned process owns bbox MMIO, including expiry, submit and stop.

No camera or AI buffers cross this process boundary. Its monotonic timestamps
share the host clock with inference, and expiry works even if inference holds
the Python GIL for seconds.
"""
import json
import multiprocessing
import signal
import threading
import time
from pathlib import Path

from smoothing import BoxSmoother


def hardware_overlay():
    from pynq import MMIO
    from overlay_driver import BboxOverlay
    return BboxOverlay(MMIO(0x41260000,0x10000),MMIO(0x41270000,0x10000),
                       MMIO(0x41280000,0x10000))


class DisplayState:
    def __init__(self, overlay, hold_s=2.0, source_max_age_s=10.0,
                 mode='raw', alpha=.5, clock=time.monotonic):
        if hold_s<=0 or source_max_age_s<=0 or mode not in ('raw','smooth'):
            raise ValueError('Invalid display timing or mode')
        self.overlay=overlay
        self.clock=clock
        self.hold_s=hold_s
        self.source_max_age_s=source_max_age_s
        self.mode=mode
        self.smoother=BoxSmoother(alpha=alpha,confirmations=1,timeout_s=hold_s)
        self.latest_id=-1
        self.latest_source=None
        self.state=dict(box_valid=False,box_commits=0,clear_commits=0,
                        source_frame_id=None,source_frame_complete_monotonic=None,
                        inference_start=None,inference_end=None,
                        overlay_commit_ack_monotonic=None,
                        latest_result_source_frame_id=None,
                        latest_result_source_frame_complete_monotonic=None,
                        latest_result_inference_start=None,latest_result_inference_end=None,
                        latest_result_has_detection=False,
                        display_mode=mode,source_max_age_s=source_max_age_s,hold_s=hold_s)

    def snapshot(self):
        now=self.clock()
        source=self.state['source_frame_complete_monotonic']
        ack=self.state['overlay_commit_ack_monotonic']
        return {**self.state,'display_status_monotonic':now,
                'result_source_age_ms':None if source is None else (now-source)*1000,
                'time_since_commit_ms':None if ack is None else (now-ack)*1000}

    def clear(self, reason):
        self.overlay.clear()
        self.state.update(box_valid=False,clear_reason=reason,
                          clear_ack_monotonic=self.clock())
        self.state['clear_commits']+=1
        self.smoother.clear()

    def tick(self):
        if not self.state['box_valid']:
            return False
        now=self.clock()
        if now-self.state['overlay_commit_ack_monotonic']>=self.hold_s:
            self.clear('display_hold_expired')
            return True
        if now-self.state['source_frame_complete_monotonic']>=self.source_max_age_s:
            self.clear('source_age_expired')
            return True
        return False

    def submit(self, message):
        self.tick()
        now=self.clock()
        source=message['source_frame_complete_monotonic']
        frame_id=message['source_frame_id']
        start,end=message['inference_start'],message['inference_end']
        if not (source<=start<=end<=now) or frame_id<0:
            return {**self.snapshot(),'accepted':False,'reject_reason':'invalid_timestamps'}
        if frame_id<=self.latest_id or (self.latest_source is not None and source<=self.latest_source):
            return {**self.snapshot(),'accepted':False,'reject_reason':'out_of_order'}
        self.latest_id,self.latest_source=frame_id,source
        self.state.update(latest_result_source_frame_id=frame_id,
            latest_result_source_frame_complete_monotonic=source,
            latest_result_inference_start=start,latest_result_inference_end=end,
            latest_result_has_detection=message['detection'] is not None)
        if now-source>=self.source_max_age_s:
            return {**self.snapshot(),'accepted':False,'reject_reason':'source_too_old'}
        raw=message['detection']
        if raw is None:
            self.clear('empty_result')
            return {**self.snapshot(),'accepted':True,'new_detection':False}
        shown=dict(raw) if self.mode=='raw' else self.smoother.observe(raw,end)
        self.overlay.set_box(shown['video_xyxy'],shown['class_id'],shown['confidence'])
        ack=self.clock()
        self.state.update(box_valid=True,source_frame_id=frame_id,
            source_frame_complete_monotonic=source,inference_start=start,inference_end=end,
            overlay_commit_ack_monotonic=ack,raw_detection=raw,displayed_detection=shown,
            frame_completion_to_overlay_ms=(ack-source)*1000,clear_reason=None)
        self.state['box_commits']+=1
        self.tick()
        if not self.state['box_valid']:
            return {**self.snapshot(),'accepted':False,'new_detection':False,
                    'reject_reason':'source_expired_during_commit'}
        return {**self.snapshot(),'accepted':True,'new_detection':True}


def _display_worker(connection, overlay_factory, settings, status_path, events_path=None):
    state=None
    stop_requested=False
    def request_stop(signum, frame):
        nonlocal stop_requested
        stop_requested=True
    signal.signal(signal.SIGTERM,request_stop)
    signal.signal(signal.SIGINT,request_stop)
    journal=None
    if events_path is not None:
        from user_recording import EventJournal
        journal=EventJournal(events_path)

    def publish():
        if status_path is not None:
            path=Path(status_path)
            temporary=path.with_suffix('.tmp')
            try:
                temporary.write_text(json.dumps(state.snapshot(),indent=2))
                temporary.replace(path)
                if journal is not None:journal.record(state.snapshot())
            except Exception as error:
                state.state['status_write_error']=repr(error)

    try:
        state=DisplayState(overlay_factory(),**settings)
        state.clear('startup')
        publish()
        connection.send({'request_id':0,'ok':True,**state.snapshot()})
    except Exception as error:
        connection.send({'request_id':0,'ok':False,'error':repr(error)})
        connection.close()
        return
    next_publish=time.monotonic()+.25
    while True:
        try:
            if state.tick(): publish()
            if stop_requested:
                state.clear('signal_stop')
                publish()
                break
        except Exception as error:
            state.state['display_error']=repr(error)
            publish()
            time.sleep(.1)
        if connection.poll(.02):
            try:
                request_id,command,payload=connection.recv()
            except EOFError:
                stop_requested=True
                continue
            try:
                if command=='stop':
                    state.clear('stop')
                    response=state.snapshot()
                elif command=='stop_after_video_halt':
                    # The owning parent has verified VDMA halt and AI idle.
                    # Do not touch MMIO again, and never fabricate a clear ACK.
                    state.state.update(clear_confirmed=False,video_halted=True,
                        display_active=False,stop_reason='video_halted_without_clear_ack')
                    response=state.snapshot()
                elif command=='submit':
                    response=state.submit(payload)
                else:
                    response=state.snapshot()
                publish()
                connection.send({'request_id':request_id,'ok':True,**response})
                if command in ('stop','stop_after_video_halt'): break
            except Exception as error:
                state.state['display_error']=repr(error)
                publish()
                try:
                    connection.send({'request_id':request_id,'ok':False,
                                     'error':repr(error),**state.snapshot()})
                except (BrokenPipeError,EOFError): stop_requested=True
        if time.monotonic()>=next_publish:
            publish()
            next_publish=time.monotonic()+.25
    connection.close()


class DisplayOwner:
    def __init__(self, hold_s=2.0, source_max_age_s=10.0, mode='raw', alpha=.5,
                 status_path=None, overlay_factory=hardware_overlay, events_path=None):
        context=multiprocessing.get_context('spawn')
        self.connection,self.child=context.Pipe()
        self.lock=threading.Lock()
        self.stopped=False
        self.started=False
        self.stop_result=None
        self.request_id=0
        self.process=context.Process(target=_display_worker,
            args=(self.child,overlay_factory,dict(hold_s=hold_s,source_max_age_s=source_max_age_s,
                                                 mode=mode,alpha=alpha),status_path,events_path),
            name='v300-display-owner')

    def start(self):
        # The caller retains this object even if startup/acknowledgement fails.
        self.process.start()
        self.started=True
        self.child.close()
        self._receive(0,15)
        return self

    def _receive(self, request_id, timeout=3.0):
        deadline=time.monotonic()+timeout
        while self.connection.poll(max(0,deadline-time.monotonic())):
            response=self.connection.recv()
            # A reply to an earlier timed-out request cannot acknowledge stop.
            if response['request_id']!=request_id: continue
            if not response['ok']: raise RuntimeError(response['error'])
            return response
        raise TimeoutError('Display owner acknowledgement timed out')

    def _request_locked(self, command, payload=None):
        if self.stopped or not self.started:
            raise RuntimeError('Display owner is not running')
        self.request_id+=1
        self.connection.send((self.request_id,command,payload))
        return self._receive(self.request_id)

    def submit(self, message):
        with self.lock: return self._request_locked('submit',message)

    def snapshot(self):
        with self.lock: return self._request_locked('snapshot')

    def stop(self):
        with self.lock:
            if self.stopped: return self.stop_result
            if self.started and self.process.is_alive():
                response=self._request_locked('stop')
                self.process.join(3)
                if self.process.is_alive():
                    raise TimeoutError('Display owner did not exit after clear acknowledgement')
                self.stop_result=dict(clear_confirmed=True,stop_reason='clear_acknowledged',
                    clear_ack_monotonic=response['clear_ack_monotonic'],
                    process_exitcode=self.process.exitcode)
            else:
                if self.started: self.process.join(0)
                self.stop_result=dict(clear_confirmed=False,
                    stop_reason='process_exited_without_clear_ack' if self.started else 'not_started',
                    process_exitcode=self.process.exitcode)
            self.stopped=True
            self.connection.close()
            self.child.close()
            return self.stop_result

    def stop_after_video_halt(self):
        """Only shutdown_once may use this after VDMA halt and AI idle checks."""
        with self.lock:
            if self.stopped: return self.stop_result
            response={}
            if self.started and self.process.is_alive():
                response=self._request_locked('stop_after_video_halt')
                self.process.join(3)
                if self.process.is_alive():
                    raise TimeoutError('Display owner did not exit after verified video halt')
                reason='video_halted_without_clear_ack'
            else:
                if self.started: self.process.join(0)
                reason='process_exited_without_clear_ack' if self.started else 'not_started'
            self.stop_result=dict(clear_confirmed=False,stop_reason=reason,
                video_halted=True,process_exitcode=self.process.exitcode,
                display_error=response.get('display_error'))
            self.stopped=True
            self.connection.close()
            self.child.close()
            return self.stop_result
