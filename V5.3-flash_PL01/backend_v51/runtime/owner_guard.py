"""Exclusive DMA owner with one-use, boot-bound cold/idle handoff evidence."""
import hashlib
from contextlib import contextmanager
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HANDOFF_PATH=Path('/run/ax7020-v300-handoff.json')
BOOT_ID_PATH=Path('/proc/sys/kernel/random/boot_id')
LOCK_PATH=Path('/run/lock/ax7020-camera-owner.lock')
PROC_ROOT=Path('/proc')
LOCKS_PATH=Path('/proc/locks')
_owner_lock=None
_startup_lock=None


def _boot_id():
    value=BOOT_ID_PATH.read_text().strip()
    if not value: raise RuntimeError('Missing current boot identity')
    return value


def _process_start_ticks(pid=None):
    raw=(PROC_ROOT/str(os.getpid() if pid is None else pid)/'stat').read_text()
    return int(raw.rpartition(')')[2].split()[19])


def _lock_still_owned(lock):
    if lock is None or lock is not _owner_lock or lock.closed:return False
    try:
        actual=os.fstat(lock.fileno());named=LOCK_PATH.stat()
        if (actual.st_dev,actual.st_ino)!=(named.st_dev,named.st_ino):return False
        for pid,major,minor,inode in re.findall(r'FLOCK\s+ADVISORY\s+WRITE\s+(\d+)\s+([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)',LOCKS_PATH.read_text()):
            if int(pid)==os.getpid() and int(major,16)==os.major(actual.st_dev) and int(minor,16)==os.minor(actual.st_dev) and int(inode)==actual.st_ino:return True
    except (OSError,AttributeError,ValueError):return False
    return False


def verify_owner_context(lock,boot,start_ticks,*,shutdown=False,closing_confirmed=False):
    if not _lock_still_owned(lock) or _boot_id()!=boot or _process_start_ticks()!=start_ticks:return False
    record=json.loads(HANDOFF_PATH.read_text())
    allowed=('ACTIVE','UNKNOWN') if shutdown and closing_confirmed else ('ACTIVE',)
    return (record.get('state') in allowed and record.get('boot_id')==boot and record.get('owner_pid')==os.getpid()
            and record.get('owner_start_ticks')==start_ticks and (not shutdown or closing_confirmed))


def _write_handoff(record):
    temporary=HANDOFF_PATH.with_suffix('.tmp')
    with temporary.open('w') as output:
        json.dump(record,output,indent=2)
        output.flush()
        os.fsync(output.fileno())
    # Publish only the complete record; ACTIVE/UNKNOWN survives a failed write.
    os.replace(temporary,HANDOFF_PATH)


def _device_owners():
    owners=[]
    for process in PROC_ROOT.glob('[0-9]*'):
        if int(process.name)==os.getpid():continue
        try:
            maps=(process/'maps').read_text()
            mapped=any(device in maps for device in ('/dev/mem','/dev/uio','/dev/dri/','/dev/xlnk'))
            targets=[]
            for fd in (process/'fd').iterdir():
                try:targets.append(os.readlink(str(fd)))
                except FileNotFoundError:pass
            opened=any(t.startswith(('/dev/mem','/dev/uio','/dev/dri/','/dev/xlnk')) for t in targets)
            if mapped or opened:owners.append(int(process.name))
        except FileNotFoundError:continue
    return owners


def acquire_owner():
    """Consume a root-established cold/idle proof before any Overlay operation."""
    global _owner_lock,_startup_lock
    import fcntl
    startup_lock=_startup_lock
    _startup_lock=None
    lock=startup_lock if startup_lock is not None else LOCK_PATH.open('a+')
    try:
        if startup_lock is None:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        boot=_boot_id()
        record=json.loads(HANDOFF_PATH.read_text())
        if record.get('boot_id')!=boot or record.get('state') not in ('COLD_BOOT_CONFIRMED','QUIESCENT','UNOWNED_BOOT_RUNTIME_LOAD'):
            raise RuntimeError('No valid cold/idle handoff for this boot; state='+repr(record.get('state')))
        owners=_device_owners()
        if owners:raise RuntimeError('Existing device owners require safe stop; pids='+repr(owners))
        lock.seek(0);lock.truncate();lock.write(str(os.getpid())+'\n');lock.flush()
        active={**record,'state':'ACTIVE','previous_state':record['state'],
                'owner_pid':os.getpid(),'owner_start_ticks':_process_start_ticks(),'activated_monotonic':time.monotonic(),
                'activated_unix_s':time.time()}
        _write_handoff(active)
        _owner_lock=lock
        return lock
    except BaseException:
        lock.close()
        raise


@contextmanager
def deployment_lock():
    """Hold the lifecycle lock through deployment without consuming idle proof.

    This is deliberately separate from acquire_owner: deployment neither owns
    hardware nor publishes ACTIVE/QUIESCENT, and must never recursively acquire
    the hardware owner's handle or interfere with its normal shutdown.
    """
    import fcntl
    lock=LOCK_PATH.open('a+')
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        boot=_boot_id()
        record=json.loads(HANDOFF_PATH.read_text())
        if record.get('boot_id')!=boot or record.get('state') not in ('COLD_BOOT_CONFIRMED','QUIESCENT','UNOWNED_BOOT_RUNTIME_LOAD'):
            raise RuntimeError('No valid cold/idle handoff for deployment; state='+repr(record.get('state')))
        owners=_device_owners()
        if owners:raise RuntimeError('Existing device owners require safe stop; pids='+repr(owners))
        yield lock
    finally:
        # Closing this descriptor cannot unlock another process's owner handle.
        lock.close()


def run_entry_locked(path):
    """Reload an entry's body under the lifecycle lock, before runtime imports.

    The entry's small __main__ header calls this helper and exits. Its normal
    body runs with the name below, and acquire_owner takes over this same lock
    only when initialization is ready to claim hardware.
    """
    global _startup_lock
    import runpy
    with deployment_lock() as lock:
        _startup_lock=lock
        try:
            return runpy.run_path(str(path),run_name='__ax7020_locked_main__')
        finally:
            _startup_lock=None


def mark_quiescent(camera, executor=None):
    """Verify the still-owned, loaded layout before publishing a reusable proof.

    Call after safe shutdown and before closing the acquire_owner handle. A
    failed verification writes UNKNOWN; a failed write leaves no new proof.
    """
    if not _lock_still_owned(_owner_lock):
        raise RuntimeError('Quiescence requires the active owner lock')
    boot=_boot_id();start_ticks=_process_start_ticks()
    try:
        previous=json.loads(HANDOFF_PATH.read_text())
    except (OSError,ValueError):
        previous={}
    unknown={'state':'UNKNOWN','boot_id':boot,'owner_pid':os.getpid(),
             'owner_start_ticks':start_ticks,
             'checked_monotonic':time.monotonic()}
    # Keep the non-reusable ACTIVE/UNKNOWN state during the owned closing audit.
    # Any failure publishes UNKNOWN below; a crash leaves that state unusable
    # for takeover. Never transiently revoke our own owner proof mid-audit.
    try:
        if (previous.get('boot_id')!=boot or previous.get('state') not in ('ACTIVE','UNKNOWN') or
                previous.get('owner_pid')!=os.getpid() or previous.get('owner_start_ticks')!=start_ticks):
            raise RuntimeError('Active handoff identity no longer matches this owner')
        from board.owned_hardware import ensure_core_guard
        guard=executor if executor is not None else ensure_core_guard(camera,shutdown=True)
        guard.verify_shutdown_owner()
        if camera.overlay is None or camera.bit_path is None:
            raise RuntimeError('No successfully loaded camera layout identity')
        loaded=getattr(camera.overlay,'is_loaded',False)
        if not (loaded() if callable(loaded) else loaded):
            raise RuntimeError('Known camera overlay is no longer loaded')
        bit=Path(camera.bit_path).resolve()
        hwh=bit.with_suffix('.hwh')
        if Path(camera.overlay.bitfile_name).resolve()!=bit:
            raise RuntimeError('Loaded overlay path differs from camera session')
        from camera_control import _validate_hwh
        # These are file/layout checks; no speculative PL address is accessed.
        _validate_hwh(hwh)
        camera._validate_overlay_ip_dict(camera.overlay.ip_dict)
        modules=ET.parse(hwh).getroot().findall('.//MODULE')
        old=any('conv_accel' in (m.get('INSTANCE','')+' '+m.get('VLNV','')) for m in modules)
        fastest=[m for m in modules if m.get('VLNV','').split(':')[-2:-1]==['fastest_accel']]
        if old or len(fastest)!=1 or fastest[0].get('INSTANCE')!='fastest_accel_0' or 'fastest_accel_0' not in camera.overlay.ip_dict:
            raise RuntimeError('PL01 quiescence requires the real new Fastest core, not neural ABSENT or old conv_accel')
        if camera.vdma_mmio is None:
            raise RuntimeError('No verified VDMA register mapping')
        values=dict(mm2s_control=int(camera.vdma_mmio.read(0)),
                    mm2s_status=int(camera.vdma_mmio.read(4)),
                    s2mm_control=int(camera.vdma_mmio.read(0x30)),
                    s2mm_status=int(camera.vdma_mmio.read(0x34)))
        if ((values['mm2s_control']|values['s2mm_control'])&1 or
                not (values['mm2s_status']&1 and values['s2mm_status']&1)):
            raise RuntimeError('VDMA is not stopped with both channels halted')
        ui=getattr(camera,'pl01_ui_mmio',None)
        if ui is not None:
            values.update(ui_control=int(ui.read(0)),ui_status=int(ui.read(4)))
            if values['ui_control']&1 or not values['ui_status']&1:raise RuntimeError('UI VDMA is not stopped/halted')
        core=guard.check_idle(shutdown=True)
        values.update(fastest_ap_ctrl=core['ap_ctrl'],fastest_phase=core['phase'])
        report=dict(state='QUIESCENT',boot_id=boot,owner_pid=os.getpid(),
                    owner_start_ticks=start_ticks,
                    checked_monotonic=time.monotonic(),checked_unix_s=time.time(),
                    bitstream=dict(path=str(bit),sha256=hashlib.sha256(bit.read_bytes()).hexdigest()),
                    hwh=dict(path=str(hwh),sha256=hashlib.sha256(hwh.read_bytes()).hexdigest()),
                    idle_registers=values,
                    proof='Known new PL01 layout: VDMA RS=0/Halted=1; fastest AP_IDLE/start0/auto0/phase0')
        _write_handoff(report)
        return report
    except BaseException as error:
        unknown['reason']=repr(error)
        _write_handoff(unknown)
        raise
