"""Signal only this V2.9.7-mini checkout's known owner; never force-kill."""
import argparse
import json
import os
import signal
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def verified_handoff(status,pid,handoff,boot_id):
    return (status.get('shutdown_verified') is True and
            status.get('handoff',{}).get('state')=='QUIESCENT' and
            handoff.get('state')=='QUIESCENT' and
            handoff.get('boot_id')==boot_id and handoff.get('owner_pid')==pid)


def stopped(status,pid):
    from owner_guard import HANDOFF_PATH,BOOT_ID_PATH
    try:
        handoff=json.loads(HANDOFF_PATH.read_text())
        return verified_handoff(status,pid,handoff,BOOT_ID_PATH.read_text().strip())
    except (OSError,ValueError):
        return False


def matches_owner(argv,cwd):
    allowed={ROOT/'run_realtime.py',ROOT/'board_acceptance.py',ROOT.parents[1]/'launch_v297_mini.py'}
    args=list(argv[1:])
    while args and args[0] in ('-B','-u'):args.pop(0)
    if not args or args[0].startswith('-'):return False
    script=Path(args[0])
    return (script if script.is_absolute() else Path(cwd)/script).resolve() in allowed


def main():
    p=argparse.ArgumentParser();p.add_argument('--status',type=Path,required=True)
    p.add_argument('--timeout',type=float,default=45)
    args=p.parse_args();status=json.loads(args.status.read_text())
    pid=int(status['owner_pid']);proc=Path('/proc')/str(pid)
    if not proc.exists():
        verified=stopped(status,pid)
        print(json.dumps({'owner_alive':False,'safe_handoff_verified':verified}))
        return 0 if verified else 2
    argv=[s.decode() for s in (proc/'cmdline').read_bytes().split(b'\0') if s]
    if not matches_owner(argv,os.readlink(str(proc/'cwd'))):
        raise RuntimeError('PID does not match this V2.9.7-mini owner; no signal sent')
    os.kill(pid,signal.SIGTERM)
    deadline=time.monotonic()+args.timeout
    while time.monotonic()<deadline:
        current=json.loads(args.status.read_text())
        if not proc.exists() and stopped(current,pid):
            print(json.dumps({'status':'SAFE_STOP_VERIFIED','owner_pid':pid,
                              'display_shutdown':current.get('display_shutdown')}));return 0
        if not proc.exists():
            print('Owner exited without verified QUIESCENT handoff; inspect status before recovery');return 2
        time.sleep(.2)
    print('Owner retained or stop pending. No force kill or overlay reload performed. Re-run this command to request another retry.')
    return 2


if __name__=='__main__':raise SystemExit(main())
