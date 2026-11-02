"""Mark a user-confirmed observation stage; does not drive the camera or change thresholds."""
import argparse
import json
import time
from pathlib import Path
def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('stage',choices=['preparation','automatic_unlabeled','no_hand','open_palm','side_palm','pinch_point','thumbs_up','thumbs_up_far','movement','hand_removed','finished'])
    p.add_argument('--confirm-ready',action='store_true',help='Use only after the user confirms readiness for this specific gesture')
    p.add_argument('--capture-unlabeled',action='store_true')
    a=p.parse_args()
    if not (a.output_dir/'realtime_status.json').exists():raise RuntimeError('No running batch at this path')
    if a.capture_unlabeled and a.stage!='automatic_unlabeled':p.error('Unlabeled capture requires automatic_unlabeled stage')
    data=dict(stage=a.stage,marked_monotonic=time.monotonic(),capture_enabled=a.confirm_ready or a.capture_unlabeled,
              manual_ready_confirmed=a.confirm_ready,label_status='USER_REQUESTED_POSE_NOT_VISUALLY_CONFIRMED' if a.confirm_ready else 'UNLABELED')
    temporary=a.output_dir/'user_phase.tmp';temporary.write_text(json.dumps(data))
    temporary.replace(a.output_dir/'user_phase.json')
    with (a.output_dir/'phase_history.jsonl').open('a') as stream:stream.write(json.dumps(data)+'\n')
    print(json.dumps(data))
if __name__=='__main__':main()
