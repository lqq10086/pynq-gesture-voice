"""Independent synthetic integer reference and target-only comparison helpers.

The host reference is not an acceptance or authorization to access hardware.
Callers must first satisfy the existing diagnostic ownership and DMA gates.
"""
import hashlib
import os
from pathlib import Path
import platform
import sys
import numpy as np


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def compare_convolutions(plan, workspace, expected):
    if np.asarray(workspace).dtype != np.int16:
        raise ValueError('workspace must contain signed 16-bit integers')
    layers = []
    for op in plan['operations']:
        if op['op'] != 0:
            continue
        key = 'conv%03d' % op['conv_index']
        if key not in expected:
            raise ValueError('missing reference convolution: ' + key)
        reference = np.asarray(expected[key])
        shape = tuple(op['output_shape'])
        start = int(op['output_offset_elements'])
        size = int(np.prod(shape))
        if reference.dtype != np.int16 or reference.shape != shape or start < 0 or start + size > workspace.size:
            raise ValueError('reference shape or workspace range differs: ' + key)
        actual = np.asarray(workspace[start:start + size]).reshape(shape)
        difference = np.abs(actual.astype(np.int32) - reference.astype(np.int32))
        layers.append(dict(conv_index=int(op['conv_index']), mismatches=int(np.count_nonzero(difference)),
                           maximum_integer_error=int(difference.max(initial=0))))
    if len({row['conv_index'] for row in layers}) != len(layers):
        raise ValueError('duplicate convolution identity')
    return dict(conv_layers=len(layers), integer_mismatches=sum(row['mismatches'] for row in layers), layers=layers)


def require_target_session(session):
    if (os.name == 'nt' or not platform.machine().lower().startswith(('arm', 'aarch')) or
            getattr(session, 'executed_on_target', False) is not True or
            getattr(session, 'scope', None) != 'PL01_OWNED_DIAGNOSTICS_ONLY' or
            not session.owner_check()):
        raise ValueError('real ARM current diagnostic owner required')


def build_host_reference(root, destination):
    """Generate a fresh deterministic synthetic A16 reference, never PASS on board."""
    root = Path(root).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('do not overwrite an existing reference')
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / 'compiler'))
    from public_accel_preflight import verify_static
    from replay_plan import read_binary_tables, read_binary_parameters, replay
    import json
    admission = verify_static(root)
    plan_folder = root / 'reports/physical_bundle_2402/plan'
    package = root / 'models/quant_2020'
    plan = json.loads((plan_folder / 'plan.json').read_text())
    maps, shifts = read_binary_tables(plan, plan_folder)
    parameters = read_binary_parameters(plan, package)
    size = int(np.prod(plan['input']['shape']))
    values = ((np.arange(size, dtype=np.uint32) * 73 + 11) % 16385).astype(np.int16)
    qinput = values.reshape(plan['input']['shape'])
    result = replay(plan, maps, shifts, parameters, qinput)
    tensors = {'images': qinput}
    for op in plan['operations']:
        if op['op'] == 0:
            tensors['conv%03d' % op['conv_index']] = result['integer_outputs'][op['output_tensor']]
    if len(tensors) != 80:
        raise ValueError('expected 79 convolution outputs')
    tensors.update(result['heads'])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        np.savez_compressed(stream, **tensors)
    return dict(status='HOST_SYNTHETIC_REFERENCE_ONLY', board_qualified=False,
                case_sha256=sha(destination), model_sha256=admission['model_sha256'],
                package_manifest_sha256=sha(package / 'manifest.json'),
                physical_plan_sha256=sha(plan_folder / 'plan.json'), convolution_outputs=79)
