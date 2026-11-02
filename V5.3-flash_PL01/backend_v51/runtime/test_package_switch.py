"""Explicit stopped-device pointer roundtrip using identical qualified weights.

No Overlay, device-register or owner-stop actions. Requires actual approval paths.
"""
import argparse
import json
from pathlib import Path
import shutil
import time
try:from .activate_package import ROOT,POINTER,DEFAULT,activate,require_stopped,validate_complete
except ImportError:from activate_package import ROOT,POINTER,DEFAULT,activate,require_stopped,validate_complete
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--numeric-approval',type=Path,required=True);parser.add_argument('--detection-approval',type=Path,required=True);args=parser.parse_args()
    if args.output.exists():raise ValueError('Evidence exists; choose new output')
    require_stopped()
    if POINTER.exists():raise ValueError('Roundtrip initializes only an absent active pointer')
    duplicate=ROOT/'packages'/('activation_roundtrip_'+str(time.time_ns()))
    original=validate_complete(DEFAULT);shutil.copytree(DEFAULT,duplicate)
    options=dict(numeric_approval=args.numeric_approval,detection_approval=args.detection_approval)
    events=[activate(DEFAULT,**options),activate(duplicate,**options),activate(rollback=True)]
    if any(e['status']!='PACKAGE_ACTIVATED_RESTART_REQUIRED' for e in events):raise RuntimeError('Owner state changed; pointer test pending safe stop')
    if any(events[-1]['active'].get(k)!=v for k,v in original.items()):raise RuntimeError('Original immutable package not restored')
    result=dict(status='POINTER_ROUNDTRIP_PASS',scope='Stopped-lock atomic activation and rollback with identical model/parameters; not new model acceptance or hardware execution',events=events,restored=original)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(result,indent=2));print(json.dumps(dict(status=result['status'],active=events[-1]['active']['path'])))
if __name__=='__main__':main()
