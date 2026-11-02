"""Safely stop this checkout or its verified V3.1 rollback application owner."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
HANDOFF_PATH=Path('/run/ax7020-v300-handoff.json')
BOOT_ID_PATH=Path('/proc/sys/kernel/random/boot_id')
PROC_ROOT=Path('/proc')

def stop(output=None):
    output=(Path(output) if output else Path(json.loads((ROOT/'reports/latest_run.json').read_text())['output'])).resolve()
    status_path=output/'status.json'
    if not status_path.exists() and (output/'summary.json').exists():
        raise RuntimeError('Benchmark summary protocol is not an application/reference stop protocol; no signal sent')
    status=json.loads(status_path.read_text())
    roles={'FORMAL_PL':'run_board_apps.py','B384_PS_FP32_REFERENCE_ONLY':'run_reference_apps.py'}
    v31=(status.get('execution_role') is None and status.get('version')=='V3.1_GESTURE_INTERACTIVE_SYSTEM')
    if v31:
        import rollback_to_v31
        stable=rollback_to_v31.STABLE
        if not any(output!=root/'reports' and output.is_relative_to((root/'reports').resolve())
                   for root in (ROOT,stable)):
            raise RuntimeError('V31 output is outside the allowed deployment reports; no signal sent')
        rollback_to_v31.verify_v31()
        entry=str(stable/'run_board_apps.py')
    elif status.get('execution_role') in roles:
        entry=str(ROOT/roles[status['execution_role']])
    else:
        raise RuntimeError('Unsupported status protocol; only formal application/reference runs can be stopped here, no signal sent')
    pid=int(status['owner_pid']);proc=PROC_ROOT/str(pid)
    if pid<=1:raise RuntimeError('Invalid application owner PID; no signal sent')
    boot=BOOT_ID_PATH.read_text().strip()
    handoff=json.loads(HANDOFF_PATH.read_text())
    if (handoff.get('state') not in ('ACTIVE','QUIESCENT') or handoff.get('owner_pid')!=pid or
            not boot or handoff.get('boot_id')!=boot):
        raise RuntimeError('PID has no matching ACTIVE/QUIESCENT owner handoff; no signal sent')
    if proc.exists():
        args=[s.decode() for s in (proc/'cmdline').read_bytes().split(b'\0') if s]
        if entry not in args or args[1:args.index(entry)] not in ([],['-B']):
            raise RuntimeError('PID does not match direct application/reference entry; no signal sent')
        if (args.count('--output')!=1 or args.index('--output')+1>=len(args) or
                Path(args[args.index('--output')+1]).resolve()!=output):
            raise RuntimeError('PID output differs from requested status directory; no signal sent')
        if handoff['state']=='ACTIVE':
            if v31:
                # Only this hash-verified fixed deployment may use its old protocol.
                subprocess.run([sys.executable,'-B',str(stable/'stop_apps.py'),
                                '--output',str(output)],cwd=stable,check=True)
            else:
                try:os.kill(pid,signal.SIGTERM)
                except ProcessLookupError:pass  # Verify the final idle proof below.
    deadline=time.monotonic()+45
    while time.monotonic()<deadline:
        status=json.loads(status_path.read_text())
        handoff=json.loads(HANDOFF_PATH.read_text()) if HANDOFF_PATH.exists() else {}
        verified=(status.get('shutdown_verified') and status.get('owner_pid')==pid and
                  (not v31 or status.get('version')=='V3.1_GESTURE_INTERACTIVE_SYSTEM') and
                  handoff.get('state')=='QUIESCENT' and
                  handoff.get('owner_pid')==pid and handoff.get('boot_id')==boot)
        if verified and not proc.exists():
            print(json.dumps(dict(status='SAFE_STOP_VERIFIED',pid=pid)))
            return
        time.sleep(.2)
    raise RuntimeError('Stop pending; buffers remain owned, no force kill performed')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output');a=p.parse_args();stop(a.output)
