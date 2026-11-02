"""Safely return this candidate's owner to the preserved qualified V5.0 runtime."""
import json,os,subprocess,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BASE=Path('/home/xilinx/jupyter_notebooks/V3.4_WB1_V5_MODELS_20261025')

def restore():
    handoff=json.loads(Path('/run/ax7020-v300-handoff.json').read_text())
    assert handoff['boot_id']==Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if handoff['state']=='ACTIVE':
        pid=int(handoff['owner_pid'])
        owner=Path(os.readlink('/proc/%d/cwd'%pid))
        command=Path('/proc/%d/cmdline'%pid).read_bytes().split(b'\0')
        assert os.fsencode(str(owner/'run_board_apps.py')) in command,'Unrecognized owner; no stop'
        if owner==BASE:
            current=json.loads((BASE/'reports/latest_run.json').read_text())
            state=json.loads((Path(current['output'])/'status.json').read_text())
            assert state['owner_pid']==pid and state['video']['healthy']
            print(json.dumps(dict(status='PREDECESSOR_ALREADY_RUNNING',pid=pid)))
            return
        assert owner==ROOT,'Unrelated owner; no stop'
        subprocess.run([sys.executable,'-B',str(ROOT/'stop_v34.py')],check=True)
        handoff=json.loads(Path('/run/ax7020-v300-handoff.json').read_text())
    assert handoff['state']=='QUIESCENT','No verified idle handoff'
    name='v50_after_v51_restore_'+str(time.time_ns())
    subprocess.run([sys.executable,'-B',str(BASE/'start_v34.py'),'--run-name',name,'--initial-mode','home',
                    '--collection-policy','production','--glass-quality','glass_fast','--gesture-trace','off',
                    '--voice-mode','exclusive'],check=True)
    print(json.dumps(dict(status='PREDECESSOR_RESTORED_EXISTING_FLOW',output=str(BASE/'reports'/name))))

if __name__=='__main__':restore()
