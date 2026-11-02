"""Freeze candidate FPGA inputs, route them, then inspect this exact new DCP."""
import hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/hardware/activation_ii1'
VIVADO=Path(r'D:\Tools\FPGA\Vivado\2023.1\bin\vivado.bat')
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def run(arguments,log):
    with log.open('w',encoding='utf-8') as stream:
        code=subprocess.run([str(x) for x in arguments],cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT).returncode
    if code:raise RuntimeError('Tool failed, see '+str(log))

def main():
    assert not OUT.exists(),'Refuse to overwrite a build'
    for name in ('sensor_wb_control.v','sensor_wb_bridge.v'):
        assert (ROOT/'hardware/rtl'/name).is_file(),name
    baseline=json.loads((ROOT/'reports/predecessor_identity/frozen_initial_hashes.json').read_text())
    for name,digest in baseline.items():
        if name.startswith(('hls/','hardware_build/hls384_D/')):
            assert sha(ROOT/name)==digest,'Frozen accelerator changed: '+name
    files=set(baseline)
    files.update(str(p.relative_to(ROOT)).replace('\\','/') for p in (ROOT/'hardware/rtl').glob('*') if p.is_file())
    files.update(('scripts/build_wb1.py','scripts/inspect_wb1_postroute.tcl'))
    inputs={name:sha(ROOT/name) for name in sorted(files)}
    OUT.mkdir(parents=True)
    (OUT/'source_manifest.json').write_text(json.dumps(inputs,indent=2))
    for name in ('hls_evidence_binding.json','export_binding.json','rtl_performance_gate.json'):
        shutil.copy2(ROOT/'reports/predecessor_identity'/name,OUT/name)
    start=time.time()
    try:
        run([VIVADO,'-mode','batch','-source',ROOT/'scripts/build_hardware_384_D.tcl',
             '-log',OUT/'vivado.log','-journal',OUT/'vivado.jou','-tclargs',OUT],OUT/'console.log')
        status=dict(returncode=0,elapsed_s=time.time()-start,
                    source_unchanged=all(sha(ROOT/n)==d for n,d in inputs.items()),
                    candidate_complete='V32_FULL_DESIGN_CANDIDATE_COMPLETE_REQUIRES_ROUTED_REVIEW' in (OUT/'build_result.txt').read_text(),
                    board='NOT_RUN',new_hardware_identity='WB1',deployment_allowed=False)
        (OUT/'status.json').write_text(json.dumps(status,indent=2))
        assert status['source_unchanged'] and status['candidate_complete']
        run([sys.executable,'-B',ROOT/'scripts/read_candidate_gate.py'],OUT/'initial_gate_console.txt')
        for name in ('inspect_v33_postroute','inspect_v33_snapshot_timing','inspect_v33_snapshot_constants','inspect_wb1_postroute'):
            run([VIVADO,'-mode','batch','-source',ROOT/'scripts'/(name+'.tcl'),
                 '-log',OUT/(name+'.log'),'-journal',OUT/(name+'.jou'),'-tclargs',OUT,sys.executable],OUT/(name+'.console.log'))
        print(json.dumps(dict(status='WB1_ROUTE_AND_INSPECTION_COMPLETE_REQUIRES_REVIEW',elapsed_s=time.time()-start,report=str(OUT))))
    except BaseException as error:
        (OUT/'failure.json').write_text(json.dumps(dict(error=repr(error),elapsed_s=time.time()-start,board='NOT_RUN'),indent=2))
        raise

if __name__=='__main__':main()
