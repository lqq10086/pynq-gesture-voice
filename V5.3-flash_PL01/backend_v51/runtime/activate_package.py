"""Atomic whole-package activation only after exact package/hardware approvals.

Never loads Overlay, stops an owner, edits handoff state, or touches registers.
Running/unknown ownership records only a pending request for normal safe stop.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET
try:
    from . import owner_guard
    from .package_contract import validate_package,HARDWARE_ABI
    from .region_executor import HARDWARE_IP
except ImportError:
    import owner_guard
    from package_contract import validate_package,HARDWARE_ABI
    from region_executor import HARDWARE_IP
ROOT=Path(__file__).resolve().parents[2]
POINTER=ROOT/'active_package.json';PENDING=ROOT/'pending_package.json';DEFAULT=ROOT/'packages/current'
HARDWARE_GATE=ROOT/'reports/mini_hardware_gate.json';HARDWARE_DIR=ROOT/'deploy/v297_mini/hardware'
IDENTITY_KEYS=('package_manifest_sha256','model_sha256','architecture_id','hardware_abi','bitstream_sha256','hwh_sha256')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def _digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def validate_complete(folder):
    folder=Path(folder).resolve();p=validate_package(folder);m=p['manifest']
    if m.get('test_only_mutation'):raise ValueError('Diagnostic mutation cannot activate for production')
    if 'calibration_manifest.json' not in p['blobs']:raise ValueError('Complete package requires hashed calibration provenance')
    return dict(path=str(folder),package_manifest_sha256=sha(folder/'model_manifest.json'),model_sha256=m['model_sha256'],architecture_id=m['architecture_id'],hardware_abi=m['hardware_abi'],
                tensor_contract_sha256=_digest(dict(shapes=p['tensor_shapes'],bits=p['tensor_bits'])),precision_contract_sha256=_digest(json.loads(p['blobs']['layer_config.json'])),calibration_sha256=sha(folder/'calibration_manifest.json'))

def validate_hardware(gate_path=HARDWARE_GATE,hardware_dir=HARDWARE_DIR):
    gate_path=Path(gate_path).resolve();folder=Path(hardware_dir).resolve();gate=json.loads(gate_path.read_text())
    if gate.get('status')!='PASS' or gate.get('facts',{}).get('accel_vlnv')!=HARDWARE_IP:raise ValueError('Qualified complete IP1.9 hardware gate required')
    bit=folder/'camera_v297_mini.bit';hwh=bit.with_suffix('.hwh')
    for p in (bit,hwh):
        info=gate['artifacts'][p.name]
        if p.stat().st_size!=info['bytes'] or sha(p)!=info['sha256']:raise ValueError('Qualified hardware payload mismatch: '+p.name)
    text=hwh.read_text(encoding='utf-8-sig');ET.fromstring(text)
    if HARDWARE_IP not in text:raise ValueError('Hardware capability IP identity mismatch')
    return dict(hardware_gate=dict(path=str(gate_path),sha256=sha(gate_path)),hardware_dir=str(folder),bitstream_sha256=sha(bit),hwh_sha256=sha(hwh))

def approval_identity(record,hardware):
    return {**{k:record[k] for k in IDENTITY_KEYS[:4]},**{k:hardware[k] for k in IDENTITY_KEYS[4:]}}

def validate_approvals(record,hardware,numeric_approval,detection_approval):
    expected=approval_identity(record,hardware);result={}
    for kind,path,statuses in [('numeric',numeric_approval,('NUMERIC_PASS',)),('detection',detection_approval,('DETECTION_REGRESSION_PASS','ENGINEERING_ACCEPTED_CURRENT_MODEL_LIMIT_RECORDED'))]:
        if path is None:raise ValueError('Package activation needs both package-specific numeric and detection approvals')
        path=Path(path).resolve();report=json.loads(path.read_text())
        if report.get('status') not in statuses or any(report.get(k)!=v for k,v in expected.items()):raise ValueError(kind+' approval identity/status differs from package or hardware')
        result[kind]=dict(path=str(path),sha256=sha(path),status=report['status'])
    return dict(hardware=hardware,approvals=result)

def validate_record(record):
    actual=validate_complete(record['path'])
    if any(record.get(k)!=v for k,v in actual.items()):raise ValueError('Recorded immutable package changed')
    qualified=record.get('qualification')
    if not qualified:raise ValueError('Recorded package lacks package-specific qualification')
    previous=qualified['hardware']
    hardware=validate_hardware(previous['hardware_gate']['path'],previous['hardware_dir'])
    if hardware!=previous:raise ValueError('Recorded qualified hardware changed')
    reports=qualified['approvals']
    for report in reports.values():
        if sha(report['path'])!=report['sha256']:raise ValueError('Recorded approval file changed')
    verified=validate_approvals(actual,hardware,reports['numeric']['path'],reports['detection']['path'])
    if verified!=qualified:raise ValueError('Recorded qualification changed')
    return {**actual,'qualification':verified}

@contextmanager
def _exclusive_device_lock():
    import fcntl
    # Same lock inode used by the normal owner. Never delete or rewrite it.
    with owner_guard.LOCK_PATH.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:yield
        finally:fcntl.flock(lock,fcntl.LOCK_UN)

def require_stopped():
    try:record=json.loads(owner_guard.HANDOFF_PATH.read_text())
    except (OSError,ValueError) as error:raise RuntimeError('Missing/invalid handoff; normal verified safe stop required') from error
    if record.get('state')!='QUIESCENT' or record.get('boot_id')!=owner_guard._boot_id():raise RuntimeError('Activation requires verified QUIESCENT handoff from this boot')
    owners=owner_guard._device_owners()
    if owners:raise RuntimeError('Device owners require normal safe stop: '+repr(owners))
    return record

def _atomic(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+str(os.getpid())+'.tmp')
    try:
        with temporary.open('w') as out:json.dump(value,out,indent=2);out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()

def activate(package=None,rollback=False,pointer=POINTER,pending=PENDING,*,numeric_approval=None,detection_approval=None,hardware_gate=HARDWARE_GATE,hardware_dir=HARDWARE_DIR):
    pointer=Path(pointer);old=json.loads(pointer.read_text()) if pointer.exists() else None
    active=validate_record(old['active']) if old else validate_complete(DEFAULT)
    if rollback:
        if not old or not old.get('previous'):raise ValueError('No previous complete package')
        if numeric_approval is None and detection_approval is None:chosen=validate_record(old['previous'])
        else:chosen=validate_complete(old['previous']['path'])
    else:
        if package is None:raise ValueError('Missing package path')
        chosen=validate_complete(package)
    hardware=validate_hardware(hardware_gate,hardware_dir)
    if 'qualification' not in chosen:
        chosen={**chosen,'qualification':validate_approvals(chosen,hardware,numeric_approval,detection_approval)}
    elif chosen['qualification']['hardware']!=hardware:raise ValueError('Rollback requires the same qualified hardware identity')
    pending_record=dict(status='PENDING_SAFE_STOP_RESTART',version=2,requested=chosen,active=active,requested_unix_s=time.time(),instruction='Use normal safe_stop; then retry activation and restart normally. No owner or hardware was changed.')
    try:
        with _exclusive_device_lock():
            stopped=require_stopped()
            # Recheck all bytes under the owner lock before the atomic pointer write.
            chosen=validate_record(chosen)
            current=json.loads(pointer.read_text()) if pointer.exists() else None
            if current!=old:raise ValueError('Active package pointer changed while validating; retry')
            active=validate_record(old['active']) if old else validate_complete(DEFAULT)
            if chosen==active and old:return old
            result=dict(status='PACKAGE_ACTIVATED_RESTART_REQUIRED',version=2,active=chosen,previous=active if old and chosen!=active else None,activated_unix_s=time.time(),quiescent_boot_id=stopped['boot_id'])
            _atomic(pointer,result)
            return result
    except (BlockingIOError,RuntimeError) as error:
        pending_record['reason']=str(error);_atomic(pending,pending_record);return pending_record

def main():
    parser=argparse.ArgumentParser();group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--package',type=Path);group.add_argument('--rollback',action='store_true');parser.add_argument('--check',action='store_true')
    parser.add_argument('--numeric-approval',type=Path);parser.add_argument('--detection-approval',type=Path);args=parser.parse_args()
    if args.check:
        record=validate_record(json.loads(POINTER.read_text())['previous']) if args.rollback else validate_complete(args.package)
        result=dict(status='PACKAGE_VALIDATED_NO_POINTER_OR_HARDWARE_WRITE',package=record)
    else:result=activate(args.package,args.rollback,numeric_approval=args.numeric_approval,detection_approval=args.detection_approval)
    print(json.dumps(result,indent=2))
if __name__=='__main__':main()
