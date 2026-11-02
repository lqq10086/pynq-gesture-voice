"""Main-thread voice/UI bridge; audio errors never own or stop video hardware."""
from collections import deque
import json
from pathlib import Path
import time

class VoiceBridge:
    def __init__(self,app,root,mode='exclusive',session_factory=None,config=None):
        from voice_control.session import InferenceGate,KwsSession
        self.app,self.root,self.mode=app,Path(root),mode
        self.config=(json.loads((self.root/'configs/voice.json').read_text()) if config is None else dict(config))
        self.config['policy']='exclusive' if mode=='off' else mode
        self.factory=session_factory or KwsSession
        self.gate=InferenceGate()
        self.session=None
        self.error=None
        self.last_command=''
        self.last_result=''
        self.records=deque(maxlen=2048)
        self.records_dropped=0
        self._counter=0
        if mode!='off': self._open()

    def _open(self):
        if self.session is not None:
            try: self.session.stop()
            except Exception as error:
                self.error=repr(error)
                return
        try:
            self.session=self.factory(self.config,self.root,gate=self.gate)
            self.session.start()
            self.error=None
        except Exception as error:
            self.error=repr(error)
            if self.session is not None:
                try: self.session.stop()
                except Exception as stop_error: self.error+='; '+repr(stop_error)
            self.session=None

    @property
    def should_pause_gesture(self):
        return bool(self.session and not self.session.error and self.session.should_pause_gesture)

    def set_listening(self,value):
        if self.mode=='off': return
        if value and (self.session is None or self.session.error): self._open()
        if self.session: self.session.set_listening(bool(value))

    def _record(self,value):
        if len(self.records)==self.records.maxlen: self.records_dropped+=1
        self.records.append(value)

    def tick(self,now=None):
        supplied_now=now
        now=time.monotonic() if supplied_now is None else supplied_now
        request=self.app.pop_voice_request()
        if request: self.set_listening(request=='start')
        if self.session:
            while True:
                event=self.session.poll()
                if event is None: break
                now=time.monotonic() if supplied_now is None else supplied_now
                self._counter+=1
                began=time.monotonic()
                result=self.app.consume_voice_command(event['label'],now=now,
                    event_id=self._counter,
                    created_monotonic=event['created_at'],
                    ttl_s=self.config.get('max_event_age_ms',200)/1000.)
                if result.get('accepted') and event['label']=='stop': self.set_listening(False)
                self.last_command=event['label']
                self.last_result=result.get('reason') or result.get('action','')
                record=dict(kind='dispatch',event=event,result=result,ui_monotonic=now,
                    event_wait_ms=(now-event['created_at'])*1000,
                    ui_process_ms=(time.monotonic()-began)*1000)
                if event.get('utterance_ended_at') is not None:
                    record['speech_end_to_ui_ms']=(now-event['utterance_ended_at'])*1000
                self._record(record)
            for observation in self.session.drain_observations():
                self._record(dict(kind='recognition',observation=observation))
        state=self.snapshot()
        self.app.set_voice_status(policy=self.mode,state=state['state'],
            listening=state['listening'],last_command=self.last_command,result=self.last_result)

    def snapshot(self):
        error=self.error or (self.session.error if self.session else None)
        listening=bool(self.session and self.session.listening and not error)
        state=('DISABLED' if self.mode=='off' else 'UNAVAILABLE' if error else
               'LISTENING' if listening else 'READY')
        return dict(policy=self.mode,state=state,error=error,listening=listening,
            last_command=self.last_command,last_result=self.last_result,
            should_pause_gesture=self.should_pause_gesture,
            stats=self.session.stats if self.session else {},records_dropped=self.records_dropped)

    def stop(self):
        if self.session:
            try: self.session.stop()
            except Exception as error: self.error=repr(error)
