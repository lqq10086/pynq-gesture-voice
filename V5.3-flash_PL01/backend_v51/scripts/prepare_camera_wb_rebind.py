"""Archive verified actual-project inputs; never create board or human PASS."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runtime'))
import wb_profile as wb


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(args):
    root = args.root.resolve()
    output = (args.output if args.output.is_absolute() else root / args.output).resolve()
    if root / 'reports' not in output.parents:
        raise ValueError('Proof output must stay below candidate backend reports')
    headers, inventory = wb._compiled_inventory(args.compiled_inventory.read_bytes())
    if headers['DCP_SHA256'] != [sha(args.checkpoint)]:
        raise ValueError('Actual routed DCP differs from compiled inventory')
    if headers['INSPECTOR_SHA256'] != [sha(args.inspector)]:
        raise ValueError('Actual collector differs from compiled inventory')
    project = Path(headers['PROJECT'][0])
    if headers['PROJECT'][1] != sha(project):
        raise ValueError('Actual Vivado project differs from compiled inventory')
    target = dict(bitstream_sha256=sha(args.bit), hwh_sha256=sha(args.hwh))
    for source, suffix in ((args.bit, '.bit'), (args.hwh, '.hwh')):
        installed = root / 'runtime/hardware' / ('camera_v32_384' + suffix)
        if sha(installed) != sha(source):
            raise ValueError('Root must install the verified new pair locally before preparing WB proof')
    accepted_raw = (root / wb.ORIGINAL_ACCEPTANCE).read_bytes()
    if hashlib.sha256(accepted_raw).hexdigest() != wb.ORIGINAL_ACCEPTANCE_SHA256:
        raise ValueError('Original human acceptance source changed')
    accepted = json.loads(accepted_raw)['accepted_profile']
    parameters = {**accepted, **target}
    output.mkdir(parents=True, exist_ok=False)

    def snapshot(source, name):
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return dict(file=destination.relative_to(root).as_posix(), sha256=sha(destination))

    proof = dict(schema=wb.CAMERA_WB_REBIND_SCHEMA, scope='CAMERA_WB_CHAIN_ONLY',
        accelerator_changed=True, human_acceptance=dict(file=wb.ORIGINAL_ACCEPTANCE,
            sha256=wb.ORIGINAL_ACCEPTANCE_SHA256, hardware=wb.REBIND_FROM),
        chain_baseline_hardware=wb.REBIND_TO, to_hardware=target,
        baseline_manifest=snapshot(args.baseline_manifest, 'baseline_manifest.json'),
        baseline_hwh=snapshot(args.baseline_hwh, 'baseline.hwh'),
        build_manifest=snapshot(args.build_manifest, 'build_manifest.json'),
        checkpoint_sha256=sha(args.checkpoint),
        compiled_inventory=snapshot(args.compiled_inventory, 'compiled_sources.tsv'),
        inspector=snapshot(args.inspector, 'inspector.tcl'), project=snapshot(project, 'project.xpr'),
        sources=[], generated_bd_sources=[])
    baseline = json.loads(args.baseline_manifest.read_bytes())['files']
    required = {'hardware/rtl/' + name for name in wb.CAMERA_WB_COMPILED_NAMES}
    required.update(name for name in baseline if name.startswith('hardware/rtl/') and name.endswith(('.mem', '.vh')))
    for logical in sorted(required):
        matches = [(kind, path, digest) for (kind, path), digest in inventory.items()
                   if path.replace('\\', '/').endswith('/' + logical)
                   and (not logical.endswith('.v') or kind == 'COMPILED')]
        if not matches or len({digest for _, _, digest in matches}) != 1:
            raise ValueError('Missing or conflicting actual camera/WB source: ' + logical)
        kind, path, digest = matches[0]
        if sha(path) != digest:
            raise ValueError('Actual source changed after inventory collection: ' + path)
        proof['sources'].append(dict(logical_path=logical, inventory_path=path, inventory_kind=kind,
                                    **snapshot(path, 'sources/' + Path(logical).name)))
    wrappers = [(kind, path, digest) for (kind, path), digest in inventory.items()
                if path.endswith('/system_camera_control_0_0/synth/system_camera_control_0_0.v')]
    if not wrappers or len({digest for _, _, digest in wrappers}) != 1:
        raise ValueError('Missing or conflicting actual generated camera BD source')
    kind, path, digest = wrappers[0]
    if sha(path) != digest:
        raise ValueError('Actual generated BD source changed after inventory collection')
    proof['generated_bd_sources'].append(dict(inventory_path=path, inventory_kind=kind,
        **snapshot(path, 'generated/system_camera_control_0_0.v')))
    wb._camera_wb_chain_evidence(root, proof, parameters)
    proof_path = output / 'proof.json'
    proof_path.write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    profile = dict(status='ACCEPTED_WB_PROFILE', **parameters,
        validation_file=wb.ORIGINAL_ACCEPTANCE, validation_sha256=wb.ORIGINAL_ACCEPTANCE_SHA256,
        hardware_rebind=dict(schema=wb.CAMERA_WB_REBIND_SCHEMA, from_hardware=wb.REBIND_FROM,
            to_hardware=target, provenance_file=proof_path.relative_to(root).as_posix(),
            provenance_sha256=sha(proof_path)))
    (output / 'profile_patch.json').write_text(json.dumps(profile, indent=2) + '\n', encoding='utf-8')
    return dict(status='CAMERA_WB_PROOF_PREPARED_REQUIRES_QUALIFICATION_BINDING',
        proof_file=str(proof_path), proof_sha256=sha(proof_path), source_count=len(proof['sources']),
        scope='Camera/WB source and endpoint continuity only; accelerator changed; no new human or board acceptance')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    for name in ('compiled-inventory', 'inspector', 'checkpoint', 'build-manifest',
                 'baseline-manifest', 'baseline-hwh', 'bit', 'hwh', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args()), indent=2))


if __name__ == '__main__':
    main()
