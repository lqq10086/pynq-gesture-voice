"""Bind the NEW passing route to candidate runtime; never creates numeric PASS."""
import argparse,ast,hashlib,json,re,shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/hardware/activation_ii1'
VERSION='V3.4_WB1_20261018'
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--numeric-report',default='reports/wb1_numeric/summary.json')
    args=parser.parse_args()
    numeric=(ROOT/args.numeric_report).resolve()
    assert (ROOT/'reports').resolve() in numeric.parents and numeric.name=='summary.json'
    numeric_relative=numeric.relative_to(ROOT).as_posix()
    gate_path=ROOT/'reports/VIDEO_HARDWARE_GATE.json'
    gate=json.loads(gate_path.read_text())
    assert gate['status']=='WB1_ROUTED_TIMING_CDC_PASS' and gate['deployment_allowed'] is True
    previous=json.loads((ROOT/'reports/predecessor_identity/runtime_qualification.json').read_text())
    for suffix,key in (('.bit','bit'),('.hwh','hwh')):
        source=OUT/('camera_v32_384'+suffix)
        assert sha(source)==gate['hardware_sha256'][key]
        assert sha(source)!=previous['fixed']['runtime/hardware/camera_v32_384'+suffix]
        shutil.copy2(source,ROOT/'runtime/hardware'/source.name)
    fixed={name:sha(ROOT/name) for name in previous['fixed']}
    path=ROOT/'runtime/qualification.py'
    text=path.read_text()
    tree=ast.parse(text)
    names=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SOURCES' for t in n.targets))
    names=tuple(dict.fromkeys((*names,'runtime/sensor_wb.py','runtime/wb_profile.py','runtime/wb_maintenance.py',
        'scripts/sensor_wb_cli.py','start_v34.py','scripts/restore_v34.py')))
    text=re.sub(r"^VERSION=.*$",'VERSION='+repr(VERSION),text,flags=re.M)
    text=re.sub(r"^NUMERIC_REPORT=.*$",'NUMERIC_REPORT='+repr(numeric_relative),text,flags=re.M)
    text=re.sub(r"^FIXED=.*$",'FIXED='+repr(fixed),text,flags=re.M)
    text=re.sub(r"^SOURCES=.*$",'SOURCES='+repr(names),text,flags=re.M)
    path.write_text(text,encoding='utf-8')
    record=dict(version=VERSION,fixed=fixed,sources={name:sha(ROOT/name) for name in names},
        hardware_gate_sha256=sha(gate_path),
        predecessor_qualification_sha256=sha(ROOT/'reports/predecessor_identity/runtime_qualification.json'),
        provenance='Independent WB1 control-interface build; frozen HLS/model reused byte-for-byte; own route and board numeric gates required',
        qualification='STARTUP_IDENTITY_ONLY; '+numeric_relative+' must pass on this exact bit/HWH')
    (ROOT/'runtime/runtime_qualification.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(dict(status='WB1_STARTUP_IDENTITY_BOUND_NOT_NUMERIC_ACCEPTANCE',sources=len(names),fixed=fixed)))

if __name__=='__main__':main()
