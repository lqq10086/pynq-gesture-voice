"""Background owner for the unchanged keyword session, including ``four``.

Only accepted labels cross into UI commands. The teammate's old action/target
mapping is intentionally absent here. All event deadlines use monotonic time;
the UI supplies its own perf_counter timestamp when applying a valid command.
"""
from collections import deque
import copy
import math
from pathlib import Path
import threading
import time


NAVIGATION_WORDS = frozenset(('one','two','three','four','on','off','back'))


def valid_voice_payload(payload, now=None):
    """Check in the SAME monotonic clock domain immediately before UI use."""
    if not isinstance(payload,dict) or payload.get('clock') != 'monotonic' or not isinstance(payload.get('label'),str) or payload['label'] not in NAVIGATION_WORDS:
        return False
    now = time.monotonic() if now is None else float(now)
    values = [payload.get(key) for key in ('created_at','utterance_ended_at','expires_at')]
    if not math.isfinite(now) or any(type(value) not in (int,float) or not math.isfinite(value) for value in values):
        return False
    created,ended,expires = values
    return ended <= created+.1 and created <= now+.1 and now <= expires


class NavigationVoice:
    """Initialize, capture and poll off the display/UI preparation threads.

    ``attach(ui)`` targets ``ui.command('voice', payload)``. A test callback may
    instead be supplied; it receives the same payload and should return False
    if it cannot accept it. A four-item latest bounded queue bridges startup.
    ``session_factory(config, root, gate=...)`` supports the existing hardware
    audio adapter without modifying KwsSession or its model/VAD settings.
    """
    def __init__(self, config=None, root=None, gate=None, session_factory=None):
        self.config = copy.deepcopy(config or {})
        self.config.setdefault('policy','always')
        self.config.setdefault('start_listening',True)
        self.enabled = bool(self.config.get('enabled',False))
        self.root = Path(root or Path(__file__).resolve().parents[1])
        self.gate,self._factory = gate,session_factory
        self._max_event_age = float(self.config.get('max_event_age_ms',200))/1000.
        self._deadline = float(self.config.get('command_deadline_ms',500))/1000.
        if not all(math.isfinite(value) and value >= 0 for value in (self._max_event_age,self._deadline)):
            raise ValueError('voice freshness bounds must be finite and nonnegative')
        self._condition = threading.Condition()
        self._stop = threading.Event()
        self._thread = None
        self._session,self._target = None,None
        self._events = deque(maxlen=4)
        self._sequence = self._accepted = self._delivered = self._ignored = 0
        self._stale = self._queue_drops = self._dispatch_rejects = self._dispatch_errors = 0
        self._polls = 0
        self._error = self._dispatch_error = self._last_label = None
        self._state = 'idle' if self.enabled else 'disabled'
        self._startup_ms = None
        self._listening = self._ready = self._closed = False
        self._session_stats = {}

    def attach(self, target):
        if target is not None and not callable(target) and not callable(getattr(target,'command',None)):
            raise TypeError('voice target must implement command or be a callback')
        with self._condition:
            self._target = target
            self._condition.notify_all()
        return self

    def start(self):
        with self._condition:
            if self._closed or not self.enabled:return False
            if self._thread is not None:return not self._stop.is_set()
            self._state = 'initializing'
            self._thread = threading.Thread(target=self._run,name='combined-voice-owner',daemon=True)
            self._thread.start()
        return True

    @property
    def error(self):
        with self._condition:return self._error

    @property
    def listening(self):
        with self._condition:return self._listening and not self._closed and self._error is None

    def status(self):
        """Small display-loop snapshot; historical recognitions stay off that path."""
        with self._condition:
            return dict(status=self._state,error=self._error,
                        listening=self._listening and not self._closed and self._error is None)

    def metrics(self):
        with self._condition:
            return dict(enabled=self.enabled,status=self._state,error=self._error,
                policy=self.config['policy'],started=self._session is not None,
                ready=self._ready,listening=self._listening,closed=self._closed,
                startup_ms=self._startup_ms,polls=self._polls,
                accepted_labels=self._accepted,dispatched_labels=self._delivered,
                ignored_labels=self._ignored,stale_events=self._stale,
                event_queue_drops=self._queue_drops,dispatch_rejects=self._dispatch_rejects,
                dispatch_errors=self._dispatch_errors,last_dispatch_error=self._dispatch_error,
                event_queue_size=len(self._events),event_queue_capacity=4,
                last_label=self._last_label,clock='monotonic',
                session=copy.deepcopy(self._session_stats))

    def close(self, timeout=3.):
        self._stop.set()
        with self._condition:
            self._events.clear()
            self._condition.notify_all()
            thread = self._thread
            if thread is None:
                self._closed = True
                self._state = 'closed' if self.enabled else 'disabled'
                return True
        if threading.current_thread() is not thread:
            thread.join(max(0.,float(timeout)))
        return not thread.is_alive()

    def _accept(self,event):
        label = event.get('label') if isinstance(event,dict) else None
        if not isinstance(label,str) or label not in NAVIGATION_WORDS:
            with self._condition:self._ignored += 1
            return
        created,ended = event.get('created_at'),event.get('utterance_ended_at')
        if any(type(value) not in (int,float) or not math.isfinite(value) for value in (created,ended)):
            with self._condition:self._stale += 1
            return
        payload = dict(label=label,confidence=event.get('confidence'),
                       created_at=created,utterance_ended_at=ended,
                       expires_at=min(created+self._max_event_age,ended+self._deadline),
                       clock='monotonic')
        if not valid_voice_payload(payload):
            with self._condition:self._stale += 1
            return
        with self._condition:
            self._sequence += 1
            payload['sequence'] = self._sequence
            if len(self._events) == self._events.maxlen:self._queue_drops += 1
            self._events.append(payload)
            self._accepted += 1
            self._last_label = label

    def _dispatch(self):
        for _ in range(4):
            with self._condition:
                target = self._target
                if target is None or not self._events:return
                payload = self._events.popleft()
            if not valid_voice_payload(payload):
                with self._condition:self._stale += 1
                continue
            try:
                accepted = target(payload) if callable(target) else target.command('voice',payload)
                with self._condition:
                    if accepted is False:self._dispatch_rejects += 1
                    else:self._delivered += 1
            except Exception as exc:
                with self._condition:
                    self._dispatch_errors += 1
                    self._dispatch_error = '%s: %s' % (type(exc).__name__,exc)

    def _snapshot_session(self):
        stats = self._session.stats
        with self._condition:
            self._session_stats = copy.deepcopy(stats)
            self._listening = bool(self._session.listening)
            self._ready = bool(stats.get('ready',False))
            if not self._error:
                self._state = ('complete' if self._session.eof else
                               'listening' if self._listening else 'paused')

    def _run(self):
        started = time.perf_counter()
        try:
            factory = self._factory
            if factory is None:
                from voice_control.session import KwsSession
                factory = KwsSession
            session = factory(self.config,self.root,gate=self.gate)
            with self._condition:self._session = session
            if self._stop.is_set():return
            session.start()
            with self._condition:self._startup_ms = (time.perf_counter()-started)*1000.
            last_snapshot = -float('inf')
            while not self._stop.is_set():
                if session.error:raise RuntimeError(session.error)
                for _ in range(4):
                    event = session.poll()
                    with self._condition:self._polls += 1
                    if event is None:break
                    self._accept(event)
                self._dispatch()
                now = time.monotonic()
                if now-last_snapshot >= .1:
                    self._snapshot_session()
                    last_snapshot = now
                with self._condition:
                    # Wake for a newly attached UI; all model/audio work remains
                    # inside the existing KwsSession worker.
                    if not self._stop.is_set():self._condition.wait(.01)
        except Exception as exc:
            with self._condition:
                self._error = '%s: %s' % (type(exc).__name__,exc)
                self._state = 'error'
        finally:
            if self._session is not None:
                try:
                    self._snapshot_session()
                except Exception as exc:
                    with self._condition:
                        self._error = self._error or '%s: %s' % (type(exc).__name__,exc)
                try:
                    self._session.stop()
                except Exception as exc:
                    with self._condition:
                        self._error = self._error or '%s: %s' % (type(exc).__name__,exc)
            with self._condition:
                self._listening = False
                self._closed = True
                self._events.clear()
                self._state = 'error' if self._error else 'closed'


__all__ = ['NAVIGATION_WORDS','NavigationVoice','valid_voice_payload']
