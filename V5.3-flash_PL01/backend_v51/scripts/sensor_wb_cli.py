"""Read-only WB1 live sensor snapshot; no Sensor write flags are exposed."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT / 'runtime'))
import sensor_wb


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path,required=True,help='New JSON evidence path; refuses overwrite')
    parser.add_argument('--register',type=lambda value:int(value,0),action='append',default=[])
    parser.add_argument('--full-isp',action='store_true',help='Read all AWB/CCM/Gamma/SDE/LENC whitelist bytes')
    args = parser.parse_args(argv)
    record = dict(schema='sensor-wb1-read-v1',state='PREPARED',root=str(args.root.resolve()),
                  prepared_unix_s=time.time(),values={},transactions=[])
    sensor_wb._journal(args.output,record,initial=True)
    client = None
    try:
        with sensor_wb.attach(args.root) as client:
            # Hardware write authorization is cleared; no Sensor write occurs.
            client.lock_writes()
            record['identity'] = client.identity
            addresses = {0x300a,0x300b,0x4300,0x501f,0x5000,0x5001,0x501d,
                         0x503d,0x4741,0x3406,0x5196,*args.register}
            if args.full_isp:
                for first,last in sensor_wb.READ_RANGES:
                    if first >= 0x5180: addresses.update(range(first,last+1))
            for address in sorted(addresses):
                record['values'][format(address,'04x')] = client.read8(address)
            record['wb_snapshot'] = client.snapshot()
            record['transactions'] = client.history
            record.update(state='COMPLETE',completed_unix_s=time.time(),
                          gain_sampling='two identical complete banks; not a simultaneous physical latch')
    except Exception as error:
        record.update(state='FAILED',error=str(error),completed_unix_s=time.time())
        if client is not None: record['transactions'] = client.history
        sensor_wb._journal(args.output,record)
        print(json.dumps(record,indent=2))
        return 1
    sensor_wb._journal(args.output,record)
    print(json.dumps(record,indent=2))
    return 0


if __name__ == '__main__': raise SystemExit(main())
