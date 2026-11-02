"""Record an explicitly user-confirmed physical power cycle, never infer one."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import time
import owner_guard as guard

def main():
    p=argparse.ArgumentParser();p.add_argument('--confirmed-boot-id',required=True)
    p.add_argument('--user-confirmed-power-cycle',action='store_true',required=True)
    p.add_argument('--evidence',type=Path,required=True);a=p.parse_args()
    if a.evidence.exists():raise ValueError('Use a new evidence file')
    with guard.LOCK_PATH.open('a+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if guard.HANDOFF_PATH.exists():raise RuntimeError('Existing handoff requires its normal owner/recovery flow')
        boot=guard._boot_id()
        if boot!=a.confirmed_boot_id:raise RuntimeError('Confirmation belongs to a different boot')
        owners=guard._device_owners()
        if owners:raise RuntimeError('Device owners still exist: '+repr(owners))
        record=dict(state='COLD_BOOT_CONFIRMED',boot_id=boot,owner_pid=os.getpid(),
            checked_monotonic=time.monotonic(),checked_unix_s=time.time(),device_owners=owners,
            human_confirmation='User explicitly confirmed complete power removal followed by power-on',
            scope='Physical reset and no application/device owner evidence; not a measured VDMA/HLS idle result')
        a.evidence.parent.mkdir(parents=True,exist_ok=True)
        with a.evidence.open('x') as output:
            json.dump(record,output,indent=2);output.flush();os.fsync(output.fileno())
        guard._write_handoff(record)
    print(json.dumps(record,indent=2))

if __name__=='__main__':main()
