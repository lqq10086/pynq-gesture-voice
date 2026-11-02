"""Start this candidate only after a safe owner handoff and verify real work."""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import stop_board as lifecycle

ROOT = Path(__file__).resolve().parent


def atomic(path, value):
    temporary = path.with_suffix('.%d.tmp'%os.getpid())
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    temporary.replace(path)


def preflight_owner():
    path = ROOT/'backend_v51/runtime/owner_guard.py'
    spec = importlib.util.spec_from_file_location('_launcher_owner_guard',path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    # This checks the existing lock, current-boot idle proof and unknown device
    # owners. It neither consumes the proof nor touches camera/PL registers.
    with guard.deployment_lock():
        return guard._boot_id()


def work_verified(status):
    live = status.get('pipeline',{})
    app = live.get('pipeline',{})
    return (status.get('video',{}).get('healthy') is True
            and app.get('inference',{}).get('inference_results',0)>0
            and app.get('ui',{}).get('displayed_revision',0)>0
            and status.get('hardware',{}).get('ui_plane',{}).get('presented',0)>0
            and live.get('failed_presentations',0)==0
            and not app.get('last_inference_error'))


def wait_for_start(child, record, timeout):
    output = Path(record['output'])
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if (output/'status.json').exists():
            status = lifecycle.verify_status(record,output)
            if (status.get('phase') == 'RUNNING' and work_verified(status)
                    and lifecycle.matching_handoff(record,('ACTIVE',))):
                lifecycle.verify_process(record)
                return 'RUNNING_HEALTH_VERIFIED'
            if status.get('phase') == 'FAILED':
                raise RuntimeError('Application failed: '+str(status.get('error')))
            if (status.get('phase') == 'STOPPED' and work_verified(status)
                    and status.get('shutdown_verified') is True
                    and lifecycle.matching_handoff(record,('QUIESCENT',))):
                child.wait(timeout=5)
                return 'COMPLETED_SAFE'
        if child.poll() is not None:
            raise RuntimeError('Application exited before RUNNING/AI/UI-ACK verification; see '+record['console'])
        time.sleep(.2)
    raise RuntimeError('Startup verification timed out; see '+record['console'])


def start(run_name=None, duration=None, exercise=False, timeout=180.):
    if not hasattr(os,'geteuid') or os.geteuid() != 0:
        raise RuntimeError('Run this board launcher through sudo')
    if duration is not None and (not 0 < duration < float('inf')):
        raise ValueError('Duration must be positive and finite')
    if not 0 < timeout <= 180: raise ValueError('Startup timeout must be in (0,180] seconds')
    run_name = run_name or datetime.datetime.now().strftime('interactive_%Y%m%d_%H%M%S')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',run_name):
        raise ValueError('Use one simple run directory name')
    reports = ROOT/'reports'
    reports.mkdir(exist_ok=True)
    # Serialize these two candidate launchers. Hardware acquisition still makes
    # its own independent ownership check immediately before allocation.
    import fcntl
    with Path('/run/lock/ax7020-independent-app-launch.lock').open('a+') as launch_lock:
        fcntl.flock(launch_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        boot = preflight_owner()
        output = reports/run_name
        console = reports/(run_name+'.console.log')
        if output.exists(): raise FileExistsError('Run output already exists: '+str(output))
        command = [sys.executable,'-B',str(ROOT/'board_entry.py'),'--output',str(output)]
        if duration is not None: command += ['--duration',str(duration)]
        if exercise: command.append('--exercise')
        # board_entry owns creation of output. Its console is a sibling file.
        with console.open('x') as log:
            child = subprocess.Popen(command,cwd=ROOT,env=dict(os.environ,XILINX_XRT='/usr'),
                                     stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                     start_new_session=True)
        record = dict(root=str(ROOT),pid=child.pid,boot_id=boot,output=str(output),
                      console=str(console),process_start_ticks=lifecycle.process_start_ticks(child.pid),
                      command=command,status='STARTING')
        latest = reports/'latest_run.json'
        atomic(latest,record)
        try:
            record['status'] = wait_for_start(child,record,timeout)
        except BaseException as error:
            record.update(status='START_FAILED',error=repr(error))
            atomic(latest,record)
            if child.poll() is None and lifecycle.verify_process(record):
                try: child.send_signal(signal.SIGTERM)
                except ProcessLookupError: pass
                # The child's finally block owns DMA shutdown; a timeout leaves
                # that owner alive and recoverable instead of releasing buffers.
                try: child.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    raise RuntimeError('Startup failed; graceful shutdown still pending, retained owner pid=%d; %s'
                                       %(child.pid,console)) from error
            raise
        atomic(latest,record)
        print(json.dumps(record))
        return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-name')
    parser.add_argument('--duration',type=float)
    parser.add_argument('--exercise',action='store_true')
    parser.add_argument('--timeout',type=float,default=180.)
    args = parser.parse_args()
    start(args.run_name,args.duration,args.exercise,args.timeout)
