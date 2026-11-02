"""Start the preserved predecessor after its existing gates verify identity."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
ROOT=Path('/home/xilinx/jupyter_notebooks/V3.3.4_NEW384_VOICE_INTEGRATION')
RELEASE_ROOT=Path(__file__).resolve().parents[1]
EXPECTED='f619566e28c3e6bdb9008bc49cfc4ab6c80cd8bf7536946543bca98b961c4f20'
def main():
    handoff=json.loads(Path('/run/ax7020-v300-handoff.json').read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if handoff.get('boot_id')!=boot:
        raise RuntimeError('Owner handoff does not match current boot; rollback refused')
    if handoff.get('state')=='ACTIVE':
        subprocess.run([sys.executable,'-B',str(RELEASE_ROOT/'stop_v34.py')],check=True)
    elif handoff.get('state') not in ('QUIESCENT','COLD_BOOT_CONFIRMED','UNOWNED_BOOT_RUNTIME_LOAD'):
        raise RuntimeError('No verified idle handoff; rollback refused')
    record=ROOT/'runtime/runtime_qualification.json'
    if hashlib.sha256(record.read_bytes()).hexdigest()!=EXPECTED:
        raise RuntimeError('Preserved predecessor identity changed; rollback refused')
    sys.path.insert(0,str(ROOT))
    from runtime.qualification import verify_runtime
    verify_runtime(ROOT)
    return subprocess.call([sys.executable,'-B',str(ROOT/'start_v334.py'),
        '--run-name','v34_rollback_'+str(time.time_ns()),'--initial-mode','home',
        '--collection-policy','production','--glass-quality','glass_fast','--gesture-trace','off',
        '--voice-mode','exclusive'])
if __name__=='__main__': raise SystemExit(main())
