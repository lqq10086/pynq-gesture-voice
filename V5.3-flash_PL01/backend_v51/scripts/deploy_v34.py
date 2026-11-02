"""Deploy the independent WB1 candidate under the shared board owner lock."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import tarfile
sys.dont_write_bytecode=True

def collect_files(root):
    files=[]
    for name in ('apps/gesture_app','runtime','model_package','configs','native','voice_control',
                 'voice_assets','reports/new384_board_inputs','audio_support/bin',
                 'audio_support/modules','audio_support/licenses','scripts','model_source'):
        files.extend(p for p in (root/name).rglob('*') if p.is_file()
                     and '__pycache__' not in p.parts and p.suffix != '.pyc')
    files.extend(p for p in root.iterdir() if p.is_file())
    files.extend(root/n for n in ('reports/VIDEO_HARDWARE_GATE.json',
        'reports/v51_hardware_reuse.json','reports/v51_v50_VIDEO_HARDWARE_GATE.json',
        'audio_support/module_manifest.json',
        'audio_support/usb_audio_sources.zip','audio_support/SOURCE_AND_LICENSES.md',
        'audio_support/source_manifest.json','audio_support/license_source_manifest.json'))
    files.append(root/'reports/field_wb/field_wb.py')
    files.extend(root/name for name in ('reports/v51_hardware_reuse.json',
                                       'reports/v51_v50_VIDEO_HARDWARE_GATE.json'))
    tree=ast.parse((root/'runtime/qualification.py').read_text())
    numeric_name=next(ast.literal_eval(node.value) for node in tree.body
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='NUMERIC_REPORT' for t in node.targets))
    numeric=(root/numeric_name).resolve()
    if (root/'reports').resolve() not in numeric.parents:
        raise ValueError('Numeric evidence must stay inside candidate reports')
    if numeric.exists(): files.append(numeric)
    profile=root/'configs/sensor_wb_profile.json'
    if profile.exists():
        validation=(root/json.loads(profile.read_text())['validation_file']).resolve()
        if (root/'reports').resolve() not in validation.parents:
            raise ValueError('Accepted WB evidence must stay inside candidate reports')
        files.append(validation)
    return sorted(set(files))

def main():
    from boardctl import ROOT,REMOTE,connect,execute
    from deployment_transaction import deploy_remote
    files=collect_files(ROOT)
    manifest={p.relative_to(ROOT).as_posix():dict(bytes=p.stat().st_size,
              sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in files}
    output=ROOT/'reports/deployment'
    output.mkdir(parents=True,exist_ok=True)
    archive=output/'payload.tar.gz'
    info=output/'manifest.json'
    info.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    with tarfile.open(archive,'w:gz') as tar:
        for p in files: tar.add(p,arcname=p.relative_to(ROOT).as_posix(),recursive=False)
        tar.add(info,arcname='deploy_manifest.json')
    with connect() as c:
        code,out=deploy_remote(c,execute,ROOT,REMOTE,archive)
    (output/'result.json').write_text(out,encoding='utf-8')
    print(out)
    return code

if __name__=='__main__': raise SystemExit(main())
