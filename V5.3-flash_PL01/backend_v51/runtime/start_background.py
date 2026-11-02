"""Start one explicit demo batch without an SSH-dependent controlling shell."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=['ps_only','hybrid'],default='ps_only')
    p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--record-user-inputs',action='store_true');a=p.parse_args()
    output=a.output_dir.resolve();output.mkdir(parents=True,exist_ok=False)
    command=[sys.executable,'-B',str(ROOT/'launch_v297_mini.py'),'--mode',a.mode,'--output-dir',str(output)]
    if a.record_user_inputs:command.append('--record-user-inputs')
    environment=dict(os.environ,XILINX_XRT='/usr')
    with (output/'owner_console.txt').open('x') as log:
        child=subprocess.Popen(command,cwd=ROOT,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    print(json.dumps(dict(status='PROCESS_STARTED_NOT_HEALTH_VERIFIED',pid=child.pid,mode=a.mode,output_dir=str(output))))

if __name__=='__main__':main()
