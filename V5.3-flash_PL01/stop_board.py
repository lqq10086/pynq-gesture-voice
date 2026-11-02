"""Gracefully stop only this root's boot-bound, recorded application process."""
import argparse
import json
import os
from pathlib import Path
import signal
import time

ROOT = Path(__file__).resolve().parent
PROC_ROOT = Path('/proc')
BOOT_ID_PATH = Path('/proc/sys/kernel/random/boot_id')
HANDOFF_PATH = Path('/run/ax7020-v300-handoff.json')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def process_start_ticks(pid):
    # comm can contain spaces and parentheses; field 22 follows its final ')'.
    return int((PROC_ROOT/str(pid)/'stat').read_text().rpartition(')')[2].split()[19])


def verify_record(record, output=None):
    reports = (ROOT/'reports').resolve()
    saved = Path(record['output']).resolve()
    if reports not in saved.parents or (output is not None and Path(output).resolve() != saved):
        raise RuntimeError('Output does not match this root recorded run; no signal sent')
    if Path(record['root']).resolve() != ROOT or int(record['pid']) <= 1:
        raise RuntimeError('Invalid recorded root/PID; no signal sent')
    boot = BOOT_ID_PATH.read_text().strip()
    if not boot or record['boot_id'] != boot:
        raise RuntimeError('Recorded process belongs to another boot; no signal sent')
    return saved


def verify_status(record, output):
    status = read_json(output/'status.json')
    if (status.get('version') != ROOT.name or status.get('owner_pid') != record['pid']
            or status.get('boot_id') != record['boot_id']
            or Path(status.get('output','')).resolve() != output):
        raise RuntimeError('Application status identity differs from recorded run; no signal sent')
    return status


def verify_process(record):
    pid = int(record['pid'])
    proc = PROC_ROOT/str(pid)
    if not proc.exists(): return False
    try: fields = (proc/'stat').read_text().rpartition(')')[2].split()
    except FileNotFoundError:
        if not proc.exists(): return False
        raise
    if int(fields[19]) != record['process_start_ticks']:
        raise RuntimeError('PID was reused; no signal sent')
    # A safely exited child can remain briefly in /proc with empty cmdline/cwd
    # while its parent reaps it. wait_for_stop still requires /proc to vanish.
    if fields[0] == 'Z': return False
    try: args = [part.decode() for part in (proc/'cmdline').read_bytes().split(b'\0') if part]
    except FileNotFoundError:
        if not proc.exists(): return False
        raise
    entry = str(ROOT/'board_entry.py')
    if (args.count(entry) != 1 or args[1:args.index(entry)] not in ([],['-B'])
            or args.count('--output') != 1 or args.index('--output')+1 >= len(args)
            or Path(args[args.index('--output')+1]).resolve() != Path(record['output']).resolve()
            or (proc/'cwd').resolve() != ROOT):
        # It may exit after the first stat read but before cmdline/cwd. Keep
        # rejecting live/reused PIDs; wait_for_stop still requires QUIESCENT,
        # verified shutdown and actual disappearance from /proc.
        try: final = (proc/'stat').read_text().rpartition(')')[2].split()
        except FileNotFoundError:
            if not proc.exists(): return False
            raise
        if int(final[19]) != record['process_start_ticks']:
            raise RuntimeError('PID was reused; no signal sent')
        if final[0] == 'Z': return False
        raise RuntimeError('PID command/root/output does not match this application; no signal sent')
    return True


def matching_handoff(record, states):
    handoff = read_json(HANDOFF_PATH)
    return (handoff.get('state') in states and handoff.get('owner_pid') == record['pid']
            and handoff.get('boot_id') == record['boot_id'])


def wait_for_stop(record, timeout=90.):
    output = verify_record(record)
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        status = verify_status(record,output)
        proc = PROC_ROOT/str(record['pid'])
        if (status.get('shutdown_verified') is True
                and matching_handoff(record,('QUIESCENT',)) and not proc.exists()):
            return dict(status='SAFE_STOP_VERIFIED',pid=record['pid'],output=str(output),
                        boot_id=record['boot_id'],shutdown_verified=True,handoff='QUIESCENT')
        if proc.exists(): verify_process(record)
        time.sleep(.2)
    raise RuntimeError('Stop pending: shutdown/QUIESCENT proof incomplete; owner retained, no force kill')


def stop(output=None, timeout=90.):
    record = read_json(ROOT/'reports/latest_run.json')
    output = verify_record(record,output)
    verify_status(record,output)
    alive = verify_process(record)
    if not matching_handoff(record,('ACTIVE','QUIESCENT')):
        raise RuntimeError('No matching current-boot ACTIVE/QUIESCENT owner; no signal sent')
    if alive and matching_handoff(record,('ACTIVE',)):
        try: os.kill(record['pid'],signal.SIGTERM)
        except ProcessLookupError: pass
    result = wait_for_stop(record,timeout)
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--timeout',type=float,default=90.)
    args = parser.parse_args()
    if not 0 < args.timeout <= 180: parser.error('--timeout must be in (0,180] seconds')
    stop(args.output,args.timeout)
