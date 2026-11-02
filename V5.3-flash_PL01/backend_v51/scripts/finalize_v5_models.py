"""Bind PS-only model changes; a new real-board numeric PASS is still required."""
import ast
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT.parent/'V3.4_WB1_20261018'
MODEL_SHA='4cdad59edc6b4f9c0e93e61a488c53b65127347fbe0c34e1465e28c89bb989f4'
VOICE_SHA='3ba69ce25399e3cb728463aaffa07518d69e43c985565198e93ef21357ef24ab'
NUMERIC='reports/v5_board_numeric/summary.json'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    previous=json.loads((BASE/'runtime/runtime_qualification.json').read_text())
    manifest=json.loads((ROOT/'model_package/model_manifest.json').read_text())
    assert sha(ROOT/'model_package/reference.onnx')==manifest['model_sha256']==MODEL_SHA
    assert manifest['classes']==['open_palm','pinch_point']
    assert manifest['output_shape']==[1,9072,7]
    assert sha(ROOT/'voice_assets/ds_cnn_en8.onnx')==VOICE_SHA
    for name in ('runtime/hardware/camera_v32_384.bit','runtime/hardware/camera_v32_384.hwh'):
        assert sha(ROOT/name)==previous['fixed'][name]==sha(BASE/name)
    for name in ('runtime/region_executor.py','runtime/native_scheduler.py','runtime/native_batch_scheduler.py',
                 'native/host_scheduler.c','native/libhost_scheduler.so','native/batch_scheduler.c',
                 'native/libbatch_scheduler.so','runtime/camera_control.py','runtime/owner_guard.py',
                 'hdmi_output.py','frame_color.py','runtime/wb_profile.py','runtime/sensor_wb.py',
                 'runtime/wb_maintenance.py'):
        assert sha(ROOT/name)==sha(BASE/name), 'Unrequested architecture change: '+name
    sys.path.insert(0,str(ROOT/'runtime'))
    from package_contract import validate_package
    validate_package(ROOT/'model_package')
    fixed={name:sha(ROOT/name) for name in previous['fixed']}
    sources=tuple(dict.fromkeys((*previous['sources'],
        'voice_assets/config_en8.json','voice_assets/ds_cnn_en8.onnx','voice_assets/norm_stats_en8.npz',
        'voice_assets/thresholds.json','voice_assets/PROVENANCE.json','configs/voice.json')))
    path=ROOT/'runtime/qualification.py'
    text=path.read_text()
    for key,value in [('VERSION',ROOT.name),('NUMERIC_REPORT',NUMERIC),('FIXED',fixed),('SOURCES',sources)]:
        text=re.sub(r'^'+key+r'=.*$',key+'='+repr(value),text,flags=re.M)
    ast.parse(text)
    path.write_text(text,encoding='utf-8')
    record=dict(version=ROOT.name,fixed=fixed,sources={name:sha(ROOT/name) for name in sources},
        hardware_gate_sha256=sha(ROOT/'reports/VIDEO_HARDWARE_GATE.json'),
        predecessor_qualification_sha256=sha(BASE/'runtime/runtime_qualification.json'),
        provenance='New two-class gesture and eleven-class voice PS model contracts; exact unchanged WB1 bit/HWH and executor; own real PL numeric qualification required',
        qualification='STARTUP_IDENTITY_ONLY; '+NUMERIC+' must pass for this exact model/code/bit/HWH')
    (ROOT/'runtime/runtime_qualification.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    print(json.dumps(dict(status='PS_ONLY_IDENTITY_BOUND_NOT_BOARD_ACCEPTANCE',version=ROOT.name,
        sources=len(sources),model_sha256=MODEL_SHA,voice_sha256=VOICE_SHA,
        qualification_sha256=sha(ROOT/'runtime/runtime_qualification.json'),numeric_report=NUMERIC),indent=2))


if __name__=='__main__': main()
