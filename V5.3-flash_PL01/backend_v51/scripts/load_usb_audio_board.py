"""Check system USB audio availability for the legacy board launcher."""
import argparse
import json
from pathlib import Path
import shutil


def main(optional=False):
    cards=Path('/proc/asound/cards')
    if not shutil.which('arecord'):
        status='UNAVAILABLE_ARECORD_NOT_FOUND'
    elif not cards.exists() or 'WirelessMic' not in cards.read_text():
        status='UNAVAILABLE_WIRELESS_MIC_NOT_ENUMERATED'
    else:
        status='ENUMERATED_CAPTURE_PENDING'
    print(json.dumps({'status':status}),flush=True)
    return 0 if optional or status=='ENUMERATED_CAPTURE_PENDING' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--optional',action='store_true',help='Allow gesture-only startup if audio is unavailable')
    raise SystemExit(main(parser.parse_args().optional))
