"""Check preserved V5.0 identities; explicitly bind frozen V5.1 display sources.

The default is read-only. --finalize writes startup identity only and never
creates, copies, or upgrades a board numeric result.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'V5.0'
VERSION = 'V5.1_30p'
NUMERIC = 'reports/v51_board_numeric/summary.json'
REMOTE = '/home/xilinx/jupyter_notebooks/V5.1_30p'
PREDECESSOR = 'V3.4_WB1_V5_MODELS_20261025'
BASE_QUALIFICATION_SHA256 = '490531fb271e5d3e75e7a26660201e646d27bdbf8c83a6cdd77c265174a73015'
MODEL_SHA256 = '4cdad59edc6b4f9c0e93e61a488c53b65127347fbe0c34e1465e28c89bb989f4'
VOICE_SHA256 = '3ba69ce25399e3cb728463aaffa07518d69e43c985565198e93ef21357ef24ab'
HARDWARE = {
    'runtime/hardware/camera_v32_384.bit':'b4605e38fc999af654d0773b7fb063be6c42a2727a93f69fc3f91a6a5721788e',
    'runtime/hardware/camera_v32_384.hwh':'b3127b019515d343b2ee891e6d3653532901c1c15eff11468e343d6b56d16ee0'}
HARDWARE_REUSE = 'reports/v51_hardware_reuse.json'
HARDWARE_REUSE_SHA256 = 'c9b3a4cff49f6b2b77442029017b1a41c75f3cde7c8a796558d8cf220a285441'
HARDWARE_GATE_SHA256 = '500fb6cce596d8ae51ad362e88ec14cf2d93c03c65831c9e14a133038c24d70b'
DISPLAY_CHANGES = {
    'runtime/qualification.py', 'run_board_apps.py', 'start_apps.py', 'start_v34.py',
    'runtime/wb_profile.py', 'scripts/board_new384_numeric.py',
    'scripts/restore_v34.py', 'configs/voice.json', 'reports/VIDEO_HARDWARE_GATE.json',
}
ADDED_SOURCES = (
    'runtime/frame_reader.py', 'configs/sensor_wb_profile.json',
    HARDWARE_REUSE, 'reports/v51_v50_VIDEO_HARDWARE_GATE.json',
    'scripts/deploy_v34.py', 'scripts/deployment_transaction.py',
    'scripts/boardctl.py', 'scripts/finalize_v51_identity.py',
    'START.ps1', 'STOP.ps1', 'ROLLBACK.ps1',
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tree_identity(folder):
    return {path.relative_to(folder).as_posix(): sha(path)
            for path in sorted(folder.rglob('*')) if path.is_file()
            and '__pycache__' not in path.parts and path.suffix != '.pyc'}


def check_hardware(root):
    for name,digest in HARDWARE.items():
        require(sha(root/name) == digest, 'Candidate must use the exact V4.5 independent PL video hardware: '+name)
    require(sha(root/HARDWARE_REUSE) == HARDWARE_REUSE_SHA256, 'Hardware reuse provenance changed')
    require(sha(root/'reports/VIDEO_HARDWARE_GATE.json') == HARDWARE_GATE_SHA256,
            'Candidate hardware reuse gate changed')
    gate = json.loads((root/'reports/VIDEO_HARDWARE_GATE.json').read_text())
    require(gate['provenance_file'] == HARDWARE_REUSE
            and gate['provenance_sha256'] == HARDWARE_REUSE_SHA256,
            'Candidate hardware gate does not bind the reuse provenance')
    tree = ET.parse(root/'runtime/hardware/camera_v32_384.hwh')
    modules = [m for m in tree.findall('.//MODULE') if m.get('INSTANCE') == 'conv_accel_0']
    require(len(modules) == 1 and modules[0].get('VLNV') == 'xilinx.com:hls:conv_accel:3.2',
            'Shared accelerator IP3.2 HWH identity changed')
    registers = [(r.get('NAME'),r.get('ADDRESS_OFFSET'),r.get('SIZE'))
                 for r in modules[0].findall('.//REGISTER')]
    require(len(registers) == 33 and hashlib.sha256(json.dumps(registers).encode()).hexdigest()
            == 'cde81aa70410f64a540dff25bddba8492b5faccb2548c80209765df863b7e918',
            'Shared accelerator 33-register ABI changed')
    ranges = {(int(m.get('BASEVALUE'),0),int(m.get('HIGHVALUE'),0))
              for m in tree.findall('.//MEMRANGE') if m.get('INSTANCE') == 'conv_accel_0'}
    require(ranges == {(0x43c00000,0x43c0ffff)}, 'Shared accelerator address map changed')


def check(root=ROOT, base=BASE):
    root, base = Path(root), Path(base)
    baseline_record = base / 'runtime/runtime_qualification.json'
    require(sha(baseline_record) == BASE_QUALIFICATION_SHA256,
            'Preserved V5.0 qualification identity changed')
    previous = json.loads(baseline_record.read_text(encoding='utf-8'))
    require(previous['version'] == PREDECESSOR, 'Unexpected V5.0 qualified lineage')
    for name, digest in {**previous['fixed'], **previous['sources']}.items():
        require(sha(base / name) == digest, 'Preserved V5.0 changed: ' + name)
        if name not in DISPLAY_CHANGES and name not in HARDWARE:
            require(sha(root / name) == digest, 'Unrequested non-display change: ' + name)
    fixed = {name: sha(root / name) for name in previous['fixed']}
    require(fixed == {**previous['fixed'],**HARDWARE}, 'Candidate model/package/schedule or approved hardware identity changed')
    check_hardware(root)
    require(sha(root/'reports/v51_v50_VIDEO_HARDWARE_GATE.json')
            == sha(base/'reports/VIDEO_HARDWARE_GATE.json'), 'Preserved V5.0 hardware gate changed')
    for name in ('model_package', 'voice_assets', 'reports/new384_board_inputs'):
        require(tree_identity(root / name) == tree_identity(base / name),
                'Frozen V5.0 assets changed: ' + name)
    manifest = json.loads((root / 'model_package/model_manifest.json').read_text())
    require(manifest['model_sha256'] == sha(root / 'model_package/reference.onnx') == MODEL_SHA256,
            'Two-class model identity changed')
    require(manifest['classes'] == ['open_palm', 'pinch_point']
            and manifest['input_shape'] == [1, 3, 384, 384]
            and manifest['output_shape'] == [1, 9072, 7]
            and manifest['conf_threshold'] == .60 and manifest['nms_iou'] == .45,
            'Gesture model shape/classes/thresholds changed')
    require(sha(root / 'voice_assets/ds_cnn_en8.onnx') == VOICE_SHA256,
            'Eleven-class voice model identity changed')
    for name in ('runtime/frame_reader.py',):
        require(sha(root / name) == sha(base / name), 'Capture or accepted WB changed: ' + name)
    profile = json.loads((root / 'configs/sensor_wb_profile.json').read_text())
    require(profile['gains'] == [1460, 1024, 1796]
            and sha(root / profile['validation_file']) == profile['validation_sha256']
            == sha(base / profile['validation_file']), 'Accepted WB proof changed')
    # Validate the exact, provenance-bound rebind without attaching to hardware.
    sys.path.insert(0,str(root))
    from runtime.wb_profile import _parameters
    _parameters(root,root/'configs/sensor_wb_profile.json')
    voice = json.loads((root / 'configs/voice.json').read_text())
    baseline_voice = json.loads((base / 'configs/voice.json').read_text())
    require(voice['audio']['arecord_path'] == REMOTE + '/audio_support/bin/arecord',
            'Voice capture must use the candidate root')
    voice['audio']['arecord_path'] = baseline_voice['audio']['arecord_path']
    require(voice == baseline_voice, 'Voice configuration changed beyond the candidate path')
    oracle = json.loads((root / 'reports/new384_board_inputs/manifest.json').read_text())
    require(oracle['model_sha256'] == MODEL_SHA256 and len(oracle['records']) == 4,
            'Four model-specific frozen numeric inputs required')
    native = (root / 'native/libframe_color.so').read_bytes()
    require(native[:6] == b'\x7fELF\x01\x01' and int.from_bytes(native[18:20], 'little') == 40,
            'Candidate frame color library must be a 32-bit little-endian ARM ELF')
    sources = tuple(dict.fromkeys((*previous['sources'], *ADDED_SOURCES)))
    for name in sources:
        require((root / name).is_file(), 'Missing candidate source: ' + name)
    return previous, fixed, sources


def bind_assignments(text, values):
    """Replace only the existing literal identity declarations."""
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    replacements = []
    for name, value in values.items():
        nodes = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)]
        require(len(nodes) == 1, 'Missing or ambiguous qualification declaration: ' + name)
        node = nodes[0]
        replacements.append((node.lineno - 1, node.end_lineno, name + '=' + repr(value) + '\n'))
    for start, end, replacement in sorted(replacements, reverse=True):
        lines[start:end] = [replacement]
    result = ''.join(lines)
    ast.parse(result)
    return result


def finalize(root=ROOT, base=BASE):
    root, base = Path(root), Path(base)
    previous, fixed, sources = check(root, base)
    path = root / 'runtime/qualification.py'
    text = bind_assignments(path.read_text(encoding='utf-8'),
                            dict(VERSION=VERSION, NUMERIC_REPORT=NUMERIC, FIXED=fixed, SOURCES=sources))
    hashes = {name: sha(root / name) for name in sources}
    hashes['runtime/qualification.py'] = hashlib.sha256(text.encode('utf-8')).hexdigest()
    record = dict(version=VERSION, fixed=fixed, sources=hashes,
        hardware_gate_sha256=sha(root / 'reports/VIDEO_HARDWARE_GATE.json'),
        predecessor_human_version='V5.0', predecessor_qualified_version=previous['version'],
        predecessor_qualification_sha256=sha(base / 'runtime/runtime_qualification.json'),
        hardware_reuse_provenance=HARDWARE_REUSE,
        hardware_reuse_provenance_sha256=HARDWARE_REUSE_SHA256,
        provenance='V5.1 pure PL continuous camera display on exact V4.5 independent video hardware; exact V5.0 models, package, frozen inputs, executor, native libraries and original UI; explicit accepted-WB hardware rebind; fresh candidate real PL numeric and live camera qualification required',
        qualification='STARTUP_IDENTITY_ONLY; ' + NUMERIC + ' must pass for this exact model/code/bit/HWH')
    record_path = root / 'runtime/runtime_qualification.json'
    # Prepare complete files first. An interrupted pair fails the existing hash
    # gate; no partially written identity can satisfy startup verification.
    text_temp, record_temp = path.with_suffix('.tmp'), record_path.with_suffix('.tmp')
    with text_temp.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
    record_temp.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    text_temp.replace(path)
    record_temp.replace(record_path)
    return dict(status='V51_STARTUP_IDENTITY_BOUND_NOT_BOARD_ACCEPTANCE', version=VERSION,
                sources=len(sources), qualification_sha256=sha(record_path), numeric_report=NUMERIC)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check-only', action='store_true', help='Read-only precursor check (the default)')
    mode.add_argument('--finalize', action='store_true', help='Write identity after all candidate sources and ARM native libraries are frozen')
    args = parser.parse_args()
    if args.finalize:
        result = finalize()
    else:
        _, fixed, sources = check()
        result = dict(status='V51_PRECURSOR_CHECK_PASS_NO_FILES_WRITTEN', version=VERSION,
                      sources=len(sources), fixed=fixed, numeric_report=NUMERIC)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
