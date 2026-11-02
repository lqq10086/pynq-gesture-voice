"""Explicit serial-module/HDMI exercise, never fabricated NN detections."""
import threading
import time

from combined.serial_app import SerialApp
from spatial_build.model import LEVELS,Board


class SerialExerciseApp(SerialApp):
    """Use a timed direction fixture only during the short motion-rate case."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fixture_motion_until=0.
        self.fixture_input_pause_until=0.

    def observe_inference(self,result,now):
        if now<max(self.fixture_motion_until,self.fixture_input_pause_until):return []
        return super().observe_inference(result,now)

    def tick(self,now):
        if (self.active_module=='observe' and now<self.fixture_motion_until
                and not self.active._skip_post_prepare_animation_once):
            self.spatial.start_continuous_rotation((-90.,0.),now,now+1.)
        return super().tick(now)


class SerialExercise:
    def __init__(self):
        self.steps=[(level,name) for level in LEVELS for name in
                    ('enter','wrong_check','fill','check','motion','snap','back')]
        self.index=0;self.waiting=None;self.results=[];self.baseline_counts=None
        self.error=None

    @property
    def done(self):return self.index==len(self.steps) and self.error is None

    def metrics(self):
        return dict(status='PASS' if self.done else 'RUNNING',cases_completed=self.index,
            cases_total=len(self.steps),results=list(self.results),error=self.error,
            real_gesture_acceptance='NOT_MEASURED_BY_EXPLICIT_UI_FIXTURE')

    def _apply(self,app,level,name):
        now=time.perf_counter()
        if name=='enter':
            assert app.active_module=='build' and app.stage=='levels'
            app.choose_level(level.id)
            self.baseline_counts=dict(app.metrics['module_counts']['observe'])
        elif name=='wrong_check':
            app._button('check',now)
            assert app.active_module=='build' and not app.spatial.check_result.passed
        elif name=='fill':
            assert app.metrics['module_counts']['observe']==self.baseline_counts
            app.spatial.board=Board.from_coords(level.coords)
            app.spatial.check_result=None
        elif name=='check':
            app._button('check',now)
            assert app.active_module=='observe' and app.stage=='observe'
            assert {b.coord for b in app.spatial.board.blocks}==set(level.coords)
            self.baseline_counts=dict(app.metrics['module_counts']['build'])
        elif name=='motion':
            assert app.metrics['module_counts']['build']==self.baseline_counts
            app.spatial.scale=app.spatial.target_scale=1.3
            app.fixture_motion_until=now+5.
        elif name=='snap':
            app.fixture_motion_until=0.
            app.fixture_input_pause_until=now+2.
            app.cancel_interaction(now)
            app.spatial.yaw=app.spatial.target_yaw=-16.
            app.spatial.pitch=app.spatial.target_pitch=0.
            self.snap_count=app.spatial.snap_count+1
            app.spatial.start_continuous_rotation((10.,0.),now,now+1.)
        elif name=='back':
            assert app.metrics['module_counts']['build']==self.baseline_counts
            app.fixture_motion_until=0.
            app._button('close',now)
            assert app.active_module=='build' and app.stage=='levels'
        assert app.metrics['active_ui_modules']==1 and not app.metrics['inactive_module_loaded']

    def step(self,ui,now):
        if self.done:return
        status=ui.status()
        if self.waiting is None:
            if not status['ready']:return
            level,name=self.steps[self.index]
            waiting=dict(event=threading.Event(),before=status['published_revision'],
                         started=now,error=None,target=None,
                         not_before=now+(5.2 if name=='motion' else 1.1 if name=='snap' else 0.))
            def apply(app):
                try:self._apply(app,level,name)
                except Exception as exc:waiting['error']=repr(exc)
                finally:waiting['event'].set()
            if ui.command('fixture',apply) is False:raise RuntimeError('UI fixture command rejected')
            self.waiting=waiting
            return
        waiting=self.waiting
        if waiting['error']:
            self.error=waiting['error'];raise RuntimeError(self.error)
        if now-waiting['started']>20.:raise TimeoutError('Serial fixture presentation timeout')
        if not waiting['event'].is_set() or now<waiting['not_before']:return
        packet=ui.latest()
        if packet is None or packet.revision<=waiting['before']:return
        if waiting['target'] is None:waiting['target']=packet.revision
        if status['displayed_revision']<waiting['target']:return
        level,name=self.steps[self.index]
        expected='observe' if name in ('check','motion','snap') else 'build'
        if status['app'].get('active_module')!=expected:return
        if name=='snap':
            if status['app'].get('view_snap_count') != self.snap_count:return
            if status['app'].get('view_snap_face') != 'front':return
            probe=waiting.get('snap_probe')
            if probe is None:
                probe=dict(done=threading.Event(),matches=False)
                waiting['snap_probe']=probe
                def check_presented(app):
                    pose=getattr(app,'_displayed_pose',None)
                    probe['pose']=pose
                    probe['matches']=pose is not None and all(abs(v)<=1e-9 for v in pose[:2])
                    probe['done'].set()
                if ui.command('fixture',check_presented) is False:
                    raise RuntimeError('Snap presentation probe rejected')
                return
            if not probe['done'].is_set():return
            if not probe['matches']:
                waiting.pop('snap_probe')
                return
        self.results.append(dict(level=level.id,phase=name,displayed_revision=status['displayed_revision'],
            active_module=status['app'].get('active_module'),module_epoch=status['app'].get('module_epoch'),
            module_counts=status['app'].get('module_counts'),elapsed_ms=(now-waiting['started'])*1000.))
        self.index+=1;self.waiting=None
