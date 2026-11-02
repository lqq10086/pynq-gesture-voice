"""Wait for a verified owner, request normal stop, and exec the local app."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path('/home/xilinx/jupyter_notebooks/V5.3_public_core/V5.3-flash')
OLD_ROOT = Path('/home/xilinx/jupyter_notebooks/V4.5_REDESIGN01')
OLD_UNIT = 'v45-redesign01.service'
OLD_STATUS = OLD_ROOT/'reports/service/status.json'
HANDOFF = Path('/run/ax7020-v300-handoff.json')
BOOT = Path('/proc/sys/kernel/random/boot_id')
STOP_REQUESTED = False

class Abort(RuntimeError):
    pass

def request_stop(*_):
    global STOP_REQUESTED
    STOP_REQUESTED = True

def require_running():
    if STOP_REQUESTED:
        raise Abort('Shutdown requested; no takeover')

def unit_info():
    p=subprocess.run(['systemctl','show',OLD_UNIT,'-p','MainPID','-p','ActiveState',
                      '-p','SubState','-p','ControlGroup'],capture_output=True,text=True,timeout=8,check=True)
    result=dict(line.split('=',1) for line in p.stdout.splitlines() if '=' in line)
    if not {'MainPID','ActiveState','SubState','ControlGroup'} <= result.keys():
        raise Abort('Incomplete systemd properties')
    return result

def read_json(path):
    try:
        value=json.loads(path.read_text())
        return value if isinstance(value,dict) else None
    except (OSError,ValueError):
        return None

def proc_identity(pid,cgroup):
    if pid<=1 or not cgroup.startswith('/system.slice/') or '..' in PurePosixPath(cgroup).parts:
        return False
    try:
        data=Path('/proc')/str(pid)
        args=(data/'cmdline').read_bytes().split(b'\0')
        decoded=[x.decode('utf-8') for x in args if x]
        if str(OLD_ROOT/'board_entry.py') not in decoded or '--output-dir' not in decoded:
            return False
        index=decoded.index('--output-dir')
        if len(decoded)<=index+1 or decoded[index+1]!=str(OLD_ROOT/'reports/service'):
            return False
        groups=(data/'cgroup').read_text().splitlines()
        return any(line.split(':',2)[-1]==cgroup for line in groups)
    except (OSError,UnicodeError,ValueError):
        return False

def active_matches(boot, info, status, handoff):
    try: pid=int(info['MainPID'])
    except (KeyError,TypeError,ValueError): return False
    return (pid>1 and info.get('ActiveState')=='active' and info.get('SubState')=='running'
        and status is not None and status.get('phase')=='READY'
        and status.get('boot_id')==boot and status.get('owner_pid')==pid
        and handoff is not None and handoff.get('state')=='ACTIVE'
        and handoff.get('boot_id')==boot and handoff.get('owner_pid')==pid
        and proc_identity(pid,info.get('ControlGroup','')))

def wait_for_active(boot, timeout=300):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        require_running()
        if BOOT.read_text().strip()!=boot: raise Abort('Boot changed')
        info=unit_info(); status=read_json(OLD_STATUS); handoff=read_json(HANDOFF)
        if active_matches(boot,info,status,handoff):
            # Recheck immediately to avoid acting on an unstable service identity.
            again=unit_info()
            if again==info and active_matches(boot,again,read_json(OLD_STATUS),read_json(HANDOFF)):
                return int(info['MainPID']),info['ControlGroup']
        if info.get('ActiveState')=='failed': raise Abort('Prior service failed')
        time.sleep(.5)
    raise Abort('Prior service never reached verified active state')

def cgroup_empty(cgroup):
    # All visible cgroup mounts are checked; missing directories indicate removal.
    mounts=[]
    for line in Path('/proc/mounts').read_text().splitlines():
        parts=line.split()
        if len(parts)>=3 and parts[2] in ('cgroup','cgroup2'):
            mounts.append(Path(parts[1].replace('\\040',' ')))
    if not mounts or not cgroup.startswith('/system.slice/') or '..' in PurePosixPath(cgroup).parts:
        raise Abort('Cannot verify cgroup mounts or path')
    for mount in mounts:
        path=mount/cgroup.lstrip('/')
        if not path.exists(): continue
        for node in [path,*path.rglob('*')]:
            if node.is_dir():
                f=node/'cgroup.procs'
                if f.exists() and f.read_text().strip(): return False
    return True

def stop_and_wait(boot,pid,cgroup,timeout=100):
    require_running()
    p=subprocess.run(['systemctl','stop',OLD_UNIT],capture_output=True,text=True,timeout=80)
    if p.returncode:
        raise Abort('Normal stop failed: '+p.stderr.strip())
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        require_running()
        if BOOT.read_text().strip()!=boot: raise Abort('Boot changed during shutdown')
        info=unit_info(); status=read_json(OLD_STATUS); handoff=read_json(HANDOFF)
        stopped=(info.get('ActiveState')=='inactive' and info.get('SubState')=='dead'
            and info.get('MainPID')=='0' and status is not None and status.get('phase')=='STOPPED'
            and status.get('shutdown_verified') is True and status.get('owner_pid')==pid
            and status.get('boot_id')==boot and handoff is not None and handoff.get('state')=='QUIESCENT'
            and handoff.get('boot_id')==boot and handoff.get('owner_pid')==pid)
        if stopped and not (Path('/proc')/str(pid)).exists() and cgroup_empty(cgroup):
            return handoff
        time.sleep(.5)
    raise Abort('Safe shutdown evidence incomplete; owner retained if present')

def check_quiescent_overlay(record,overlay_name):
    if not overlay_name: raise Abort('PYNQ loaded overlay metadata missing')
    bit=record.get('bitstream');hwh=record.get('hwh')
    if not isinstance(bit,dict) or not isinstance(hwh,dict): raise Abort('Artifact identity missing')
    for item in (bit,hwh):
        if not isinstance(item.get('path'),str) or not isinstance(item.get('sha256'),str):
            raise Abort('Artifact identity malformed')
    bp=Path(bit['path']); hp=Path(hwh['path']); op=Path(overlay_name)
    if not all(p.is_absolute() for p in (bp,hp,op)) or bp.resolve()!=op.resolve() or hp.resolve()!=bp.resolve().with_suffix('.hwh'):
        raise Abort('PYNQ metadata and quiescent artifact differ')
    for p,item in ((bp,bit),(hp,hwh)):
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=item['sha256']:
            raise Abort('Artifact SHA mismatch')
    idle=record.get('idle_registers'); names=('mm2s_control','mm2s_status','s2mm_control','s2mm_status')
    if not isinstance(idle,dict) or any(type(idle.get(n)) is not int for n in names):
        raise Abort('VDMA evidence missing')
    if (idle['mm2s_control']|idle['s2mm_control'])&1 or not (idle['mm2s_status']&1 and idle['s2mm_status']&1):
        raise Abort('Recorded VDMA not halted')
    has_hls=any(m.get('INSTANCE')=='conv_accel_0' for m in ET.parse(hp).getroot().findall('.//MODULE'))
    if has_hls and (type(idle.get('hls_ap_ctrl')) is not int or idle['hls_ap_ctrl']&0x85!=4):
        raise Abort('Recorded HLS state not idle')

def validate_handoff_locked(boot,pid):
    sys.path.insert(0,str(ROOT/'backend_v51/runtime'))
    import owner_guard as guard
    # This lock fails closed on an active owner, and verifies boot + no other owners.
    with guard.deployment_lock():
        require_running()
        rec=read_json(HANDOFF)
        if not rec or rec.get('state')!='QUIESCENT' or rec.get('boot_id')!=boot or rec.get('owner_pid')!=pid:
            raise Abort('Quiescent handoff changed')
        from pynq import PL
        check_quiescent_overlay(rec,PL.bitfile_name)
        if guard._device_owners(): raise Abort('Device owner appeared')

def main():
    if os.geteuid()!=0: raise Abort('root required')
    signal.signal(signal.SIGTERM,request_stop)
    signal.signal(signal.SIGINT,request_stop)
    boot=BOOT.read_text().strip()
    if not boot: raise Abort('Missing boot id')
    pid,cgroup=wait_for_active(boot)
    print('BRIDGE_ACTIVE_OWNER_VERIFIED pid='+str(pid),flush=True)
    stop_and_wait(boot,pid,cgroup)
    validate_handoff_locked(boot,pid)
    require_running()
    output=ROOT/'reports'/('autostart_'+boot+'_'+uuid.uuid4().hex)
    if output.exists(): raise Abort('Output already exists')
    print('V53_AUTOSTART_OUTPUT='+str(output),flush=True)
    # Default termination closes the last signal-to-exec race before any hardware is owned.
    signal.signal(signal.SIGTERM,signal.SIG_DFL)
    signal.signal(signal.SIGINT,signal.SIG_DFL)
    require_running()
    os.chdir(ROOT)
    os.execv(sys.executable,[sys.executable,'-B',str(ROOT/'board_entry.py'),'--output',str(output)])

if __name__=='__main__':
    try: main()
    except Exception as error:
        print('BRIDGE_FAILED '+repr(error),file=sys.stderr,flush=True)
        sys.exit(1)
