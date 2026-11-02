"""Label timed user instructions; labels are never per-frame ground truth."""
import argparse
import json
from pathlib import Path
import time

def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    target=a.output_dir/'user_phase.json';history=[]
    for stage,seconds in [('no_hand',5),('open_palm',10),('side_palm',10),('pinch_point',10),('moving',10),('hand_left',5)]:
        row=dict(stage=stage,instructed_duration_s=seconds,started_monotonic=time.monotonic(),
                 scope='Timed user instruction; not measured gesture truth')
        temporary=target.with_suffix('.tmp');temporary.write_text(json.dumps(row));temporary.replace(target)
        history.append(row);time.sleep(seconds)
    (a.output_dir/'phase_instructions.json').write_text(json.dumps(history,indent=2))
    print('Instruction phases recorded; user observations still required')

if __name__=='__main__':main()
