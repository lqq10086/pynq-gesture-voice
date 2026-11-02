import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import signal
import time

ROOT=Path(__file__).resolve().parent
HANDOFF_PATH=Path('/run/ax7020-v300-handoff.json')
BOOT_ID_PATH=Path('/proc/sys/kernel/random/boot_id')
PROC_ROOT=Path('/proc')


def preflight_owner():
    boot=BOOT_ID_PATH.read_text().strip()
    handoff=json.loads(HANDOFF_PATH.read_text())
    if not boot or handoff.get('boot_id')!=boot:
        raise RuntimeError('Owner handoff does not match current boot; no launch')
    if handoff.get('state')=='ACTIVE':
        pid=int(handoff['owner_pid'])
        live=(PROC_ROOT/str(pid)).exists()
        raise RuntimeError('ACTIVE owner pid=%d (%s); stop it safely before starting another run' %
                           (pid,'process exists' if live else 'missing process without idle proof'))
    if handoff.get('state') not in ('QUIESCENT','COLD_BOOT_CONFIRMED','UNOWNED_BOOT_RUNTIME_LOAD'):
        raise RuntimeError('No verified idle/cold owner handoff; no launch')
    return boot


def wait_for_start(child,output,boot,timeout=45):
    deadline=time.monotonic()+timeout
    status={}
    while time.monotonic()<deadline:
        path=output/'status.json'
        if path.exists():
            status=json.loads(path.read_text())
            handoff=json.loads(HANDOFF_PATH.read_text())
            matching=(status.get('owner_pid')==child.pid and handoff.get('owner_pid')==child.pid and
                      handoff.get('boot_id')==boot)
            if (matching and handoff.get('state')=='ACTIVE' and
                    status.get('status')=='RUNNING_REAL_CAMERA_HDMI_APPS'
                    and status.get('display_path')=='PL_CAMERA_NO_UI' and status.get('ui_rendering') is False
                    and status.get('video',{}).get('healthy') is True
                    and status.get('pl_camera_display',{}).get('healthy') is True
                    and status.get('hardware_healthy',True) is True):
                return 'RUNNING_HEALTH_VERIFIED'
            if (matching and handoff.get('state')=='QUIESCENT' and status.get('status')=='STOPPED' and
                    status.get('shutdown_verified') is True):
                return 'COMPLETED_SAFE'
        code=child.poll()
        if code is not None:
            raise RuntimeError('New application exited (%s), latest run preserved; output=%s status=%s' %
                               (code,output,status.get('status')))
        time.sleep(.2)
    raise RuntimeError('Startup health not confirmed; latest run preserved; pid=%d output=%s' % (child.pid,output))


def start(run_name,duration=None,initial_mode='home',diagnostics=False,glass_quality='glass_fast',collection_policy='production',gesture_trace='off',trace_images_every=0,detection_hud=False,detection_profile='baseline',voice_mode='exclusive',display_path='pl-camera'):
    if display_path!='pl-camera':raise ValueError('V5.1 supports the pure PL camera display path only')
    if Path(run_name).name!=run_name:raise ValueError('Use a single directory name')
    boot=preflight_owner()
    output=ROOT/'reports'/run_name
    output.mkdir(exist_ok=False)
    command=[sys.executable,'-B',str(ROOT/'run_board_apps.py'),'--output',str(output),'--initial-mode',initial_mode]
    if duration is not None:command+=['--duration',str(duration)]
    if diagnostics:command+=['--diagnostics']
    if detection_hud:command+=['--detection-hud']
    command+=['--glass-quality',glass_quality,'--detection-profile',detection_profile]
    command+=['--collection-policy',collection_policy]
    command+=['--gesture-trace',gesture_trace,'--trace-images-every',str(trace_images_every)]
    command+=['--voice-mode',voice_mode,'--display-path',display_path]
    with (output/'console.log').open('x') as log:
        child=subprocess.Popen(command,cwd=ROOT,env=dict(os.environ,XILINX_XRT='/usr'),
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    try:
        state=wait_for_start(child,output,boot)
    except BaseException:
        # Only the child just created here receives a graceful signal. Its own
        # lifecycle retains DMA/CMA until idle; this launcher never force-kills.
        if child.poll() is None:
            try:child.send_signal(signal.SIGTERM)
            except ProcessLookupError:pass
        raise
    result=dict(pid=child.pid,output=str(output),status=state)
    latest=ROOT/'reports/latest_run.json';temporary=latest.with_suffix('.tmp')
    temporary.write_text(json.dumps(result,indent=2));temporary.replace(latest)
    print(json.dumps(result))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run-name',required=True);p.add_argument('--duration',type=float)
    p.add_argument('--initial-mode',default='home')
    p.add_argument('--voice-mode',choices=('off','exclusive','gated'),default='exclusive')
    p.add_argument('--display-path',choices=('pl-camera',),default='pl-camera')
    p.add_argument('--collection-policy',choices=('production','diagnostic'),default='production')
    p.add_argument('--gesture-trace',choices=('off','light','detailed'),default='off')
    p.add_argument('--trace-images-every',type=int,default=0)
    p.add_argument('--detection-profile',choices=('baseline','palm_balanced'),default='baseline')
    p.add_argument('--detection-hud',action='store_true')
    p.add_argument('--diagnostics',action='store_true');p.add_argument('--glass-quality',choices=('flat','glass','glass_fast'),default='glass_fast');a=p.parse_args()
    start(a.run_name,a.duration,a.initial_mode,a.diagnostics,a.glass_quality,a.collection_policy,a.gesture_trace,a.trace_images_every,a.detection_hud,a.detection_profile,a.voice_mode,a.display_path)
