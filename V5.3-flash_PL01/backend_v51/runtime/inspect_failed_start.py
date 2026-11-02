"""Re-measure the known old layout after this specific pre-download failure.

Default is read-only. --publish atomically establishes a fresh idle proof under
the same lock only after all identity, failure-chain and register checks pass.
Never downloads an Overlay or writes MMIO. Not a generic UNKNOWN override.
"""
import argparse
import fcntl
import gc
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import xml.etree.ElementTree as ET
import owner_guard as guard

ROOT=Path(__file__).resolve().parents[2]
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def inspect(publish=False):
    if sys.flags.optimize:raise RuntimeError('Safety checks require assertions enabled')
    from pynq import PL,MMIO
    from camera_control import CameraControlSession,_validate_hwh,IDENTITY,EXPECTED_ID
    with guard.LOCK_PATH.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        handoff=json.loads(guard.HANDOFF_PATH.read_text())
        failed_path=ROOT/'reports/board_numeric_01/summary.json'
        failed=json.loads(failed_path.read_text())
        assert sha(failed_path)=='dcae8ce5d0697bef183b694aec0ffaa77f26155d7a2ff352c5afac19bef58af8'
        sources={name:sha(ROOT/name) for name in ('deploy/v297_mini/board_acceptance.py',
            'deploy/v297_mini/camera_control.py','deploy/v297_mini/owner_guard.py','deploy/v297_mini/shutdown_owner.py')}
        transported={row['file']:row['sha256'] for row in json.loads((ROOT/'reports/deployment_manifest.json').read_text())}
        assert all(transported[name]==value for name,value in sources.items())
        previous=json.loads((ROOT/'reports/baseline_hold/realtime_status.json').read_text())
        proof=previous['handoff'];boot=guard._boot_id()
        assert boot=='c292337a-164c-4e84-9cc1-17b556b5a57b'
        assert previous['owner_pid']==3143 and failed['owner_pid']==3502
        assert handoff['state']=='UNKNOWN' and handoff['boot_id']==boot
        assert handoff['owner_pid']==failed['owner_pid'] and not (Path('/proc')/str(failed['owner_pid'])).exists()
        assert failed['overlay_load_count']==0 and 'RuntimeError: No Devices Found' in failed['error']
        assert 'self._overlay_factory(str(path), download=False)' in failed['error']
        assert previous['shutdown_verified'] and proof['state']=='QUIESCENT' and proof['boot_id']==boot
        assert not (Path('/proc')/str(previous['owner_pid'])).exists()
        assert not guard._device_owners()
        bit=Path(PL.bitfile_name);hwh=bit.with_suffix('.hwh')
        assert sha(bit)==proof['bitstream']['sha256'] and sha(hwh)==proof['hwh']['sha256']
        assert Path('/sys/class/fpga_manager/fpga0/state').read_text().strip()=='operating'
        _validate_hwh(hwh)
        camera=CameraControlSession(mmio_factory=MMIO)
        ips=PL.ip_dict;camera._validate_overlay_ip_dict(ips)
        kernel=ips['conv_accel_0']
        assert int(kernel['phys_addr'])==0x43c00000 and int(kernel['addr_range'])==65536
        module=next(m for m in ET.parse(hwh).getroot().findall('.//MODULE') if m.get('INSTANCE')=='conv_accel_0')
        assert kernel['type']==module.get('VLNV')
        control=camera._make_mmio(ips['camera_control_0'])
        vdma=camera._make_mmio(ips['axi_vdma_0']);hls=camera._make_mmio(kernel)
        identity=int(control.read(IDENTITY));assert identity==EXPECTED_ID
        values=dict(mm2s_control=int(vdma.read(0)),mm2s_status=int(vdma.read(4)),
                    s2mm_control=int(vdma.read(0x30)),s2mm_status=int(vdma.read(0x34)),hls_ap_ctrl=int(hls.read(0)))
        assert not (values['mm2s_control']|values['s2mm_control'])&1
        assert values['mm2s_status']&1 and values['s2mm_status']&1
        assert values['hls_ap_ctrl']&0x85==4
        assert json.loads(guard.HANDOFF_PATH.read_text())==handoff
        result=dict(status='KNOWN_PREVIOUS_LAYOUT_IDLE_MEASURED',boot_id=boot,unknown_handoff=handoff,
                    previous_handoff=proof,failed_summary_sha256=sha(ROOT/'reports/board_numeric_01/summary.json'),
                    loaded_bitstream=dict(path=str(bit),sha256=sha(bit)),loaded_hwh=dict(path=str(hwh),sha256=sha(hwh)),
                    camera_identity=identity,idle_registers=values,checked_monotonic=time.monotonic(),
                    changed_handoff=False,mmio_writes=0,overlay_downloads=0,source_sha256=sources,
                    inspector_sha256=sha(__file__))
        del control,vdma,hls,camera
        gc.collect()
        assert guard._boot_id()==boot and not guard._device_owners()
        assert str(PL.bitfile_name)==str(bit) and sha(bit)==proof['bitstream']['sha256']
        assert sha(hwh)==proof['hwh']['sha256']
        assert json.loads(guard.HANDOFF_PATH.read_text())==handoff
        if publish:
            evidence=ROOT/'reports/failed_start_idle_recovery.json'
            with evidence.open('x') as output:
                json.dump(result,output,indent=2);output.flush();os.fsync(output.fileno())
            recovered=dict(state='QUIESCENT',boot_id=boot,owner_pid=os.getpid(),
                recovery_from_state='UNKNOWN',recovery_evidence=str(evidence),recovery_evidence_sha256=sha(evidence),
                checked_monotonic=result['checked_monotonic'],checked_unix_s=time.time(),
                bitstream=result['loaded_bitstream'],hwh=result['loaded_hwh'],idle_registers=values,
                proof='Fresh known-layout idle reads after bound pre-download environment failure; no MMIO writes/download')
            guard._write_handoff(recovered)
            result['published_handoff']=recovered;result['changed_handoff']=True
        return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    result=inspect(args.publish)
    if not args.publish:
        target=ROOT/'reports/failed_start_idle_inspection.json'
        with target.open('x') as output:json.dump(result,output,indent=2)
    print(json.dumps(result,indent=2))
