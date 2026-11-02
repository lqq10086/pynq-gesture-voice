"""Separate pre-board diagnostic admission from full new-PL production identity."""
import hashlib
import importlib.util
import json
import math
from pathlib import Path,PurePosixPath

ROOT=Path(__file__).resolve().parent
MANIFEST='backend_v51/application_identity.json'
DIAGNOSTIC_MANIFEST='reports/pl01_diagnostic_identity.json'
CACHE_REPORT='reports/pl01_cache_proof.json'
PL_REPORT='reports/pl01_board_numeric.json'
SCOPE='PL01_FASTEST_DUAL_BBOX_APPLICATION'
CRITICAL_SOURCES=('hardware_driver.py','board_identity.py','camera_qualification.py','board_entry.py','board_config.json','dual_overlay.py',
    'gesture_app/inference.py','backend_v51/runtime/owner_guard.py','board/pl_runner.py','board/pl_adapter.py','board/owned_hardware.py',
    'board/pl_batch.py','board/register_map.py','board/register_map.json','native/pl_batch.cpp','native/pl_batch.h',
    'board/cache_qualification.py','board/diagnostic_session.py','board/nn_only_overlay.py','board/static_qualification.py','board/prepare_diagnostic.py',
    'compiler/replay_plan.py','host/integer_reference.py',
    'native/fused_preprocess.py','native/fused_preprocess_v2.py','native/fused_preprocess.cpp','native/fused_preprocess_v2.cpp',
    'native/ccm_gamma_bgr_lut.bin','native/build_v2/armv7/libfused_preprocess_v2.so','board/build_pl_batch/armv7/libpl_batch.so')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def local_path(root,name):
    relative=PurePosixPath(name);target=(Path(root)/name).resolve()
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or relative.as_posix()!=name or Path(root).resolve() not in target.parents:raise ValueError('qualification path escapes project: '+str(name))
    return target

def _manifest(root,name,status,scope):
    path=root/name
    if not path.is_file():raise ValueError(name+' missing: PL01 NOT_QUALIFIED')
    record=json.loads(path.read_text())
    if record.get('version')!=root.name or record.get('status')!=status or record.get('scope')!=scope or not set(CRITICAL_SOURCES)<=set(record.get('files',{})):raise ValueError('incomplete new PL01 source qualification')
    for key,value in record['files'].items():
        if sha(local_path(root,key))!=value:raise ValueError('qualified source/artifact changed: '+key)
    return record

def verify_sources(root=ROOT):
    root=Path(root).resolve();record=_manifest(root,MANIFEST,'PL01_PRODUCTION_APPLICATION_QUALIFIED',SCOPE)
    if record.get('pl_neural_network')!='FASTEST_W8PC_A16':raise ValueError('CPU absence/old PL identity cannot qualify Fastest PL01')
    return dict(version=root.name,root=str(root),source_manifest_sha256=sha(root/MANIFEST))

def camera_module(root):
    spec=importlib.util.spec_from_file_location('_pl01_camera_qualification',Path(root)/'camera_qualification.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def verify_camera_overlay(root=ROOT):return camera_module(root).verify_camera_overlay(root)

def _offline(root):
    from hardware_scripts.build_gate import Evidence,verify_quant,verify_physical
    evidence=Evidence(root);package=verify_quant(evidence);verify_physical(evidence,package)
    return package,evidence.files

def verify_diagnostic(root=ROOT):
    # Public diagnostic admission checks the same selected routed Q1 bit/HWH
    # and the byte-identical inference payload without distributing old private
    # calibration frames. It grants no production or numeric qualification.
    from public_accel_preflight import verify_static
    root=Path(root).resolve();checked=verify_static(root);hardware=checked['camera_overlay']
    return dict(version=root.name,root=str(root),scope='PL01_OWNED_DIAGNOSTICS_ONLY',production_qualified=False,
        package_manifest_sha256=sha(root/'models/quant_2020/manifest.json'),camera_overlay=hardware,
        bitstream_sha256=hardware['bitstream_sha256'],hwh_sha256=hardware['hwh_sha256'],pl_neural_network='FASTEST_W8PC_A16',
        dcp_validation=checked['dcp_validation'],route_validation=checked['route_validation'],
        qualification_boundary='PUBLIC_STATIC_ONLY_NO_TARGET_NUMERIC_OR_CACHE_PASS')

def verify_experimental(root=ROOT):
    """Integrity admission for a user-requested trial; no numeric PASS claimed."""
    root=Path(root).resolve();result=verify_diagnostic(root)
    manifest_path=root/'backend_v51/public_accel_runtime_identity.json'
    manifest=json.loads(manifest_path.read_text())
    if manifest.get('scope')!='EXPERIMENTAL_RUNTIME_INTEGRITY_NOT_BOARD_QUALIFIED':
        raise ValueError('missing experimental source integrity scope')
    for name,value in manifest.get('files',{}).items():
        if sha(local_path(root,name))!=value:raise ValueError('experimental source changed: '+name)
    if not set(CRITICAL_SOURCES)<=set(manifest.get('files',{})):
        raise ValueError('experimental source closure incomplete')
    result.update(target_numeric='NOT_QUALIFIED',source_manifest_sha256=sha(manifest_path))
    return result

def experimental_bound_files(root=ROOT):
    root=Path(root).resolve()
    from public_accel_preflight import EXPECTED,bound_routed_files
    record=json.loads((root/'backend_v51/public_accel_runtime_identity.json').read_text())
    files=[local_path(root,name) for name in record['files']]
    files.extend(local_path(root,name) for name in EXPECTED)
    files.extend(bound_routed_files(root))
    files.extend([root/'backend_v51/public_accel_runtime_identity.json',root/'public_accel_preflight.py'])
    return sorted(set(files))

def _verify_target(root,hardware,package,manifest):
    paths=[root/CACHE_REPORT,root/PL_REPORT]
    if any(not p.is_file() for p in paths):raise ValueError('fresh PL01 target cache/numeric reports missing: production NOT_QUALIFIED')
    cache,numeric=[json.loads(p.read_text()) for p in paths]
    expected=dict(bitstream_sha256=hardware['bitstream_sha256'],hwh_sha256=hardware['hwh_sha256'],package_manifest_sha256=package,
        physical_plan_sha256=sha(root/'reports/physical_bundle_2402/plan/plan.json'),driver_sha256=sha(root/'board/pl_runner.py'),native_library_sha256=sha(root/'board/build_pl_batch/armv7/libpl_batch.so'))
    for record in (cache,numeric):
        if record.get('executed_on_target') is not True or not record.get('boot_id') or record.get('owner_pid',0)<=0:raise ValueError('target execution/owner identity absent')
        if any(record.get(k)!=v for k,v in expected.items()):raise ValueError('target proof is not bound to the new hardware/package/driver')
        if not record.get('raw_references'):raise ValueError('target proof needs SHA-bound raw evidence')
        for ref in record['raw_references']:
            if sha(local_path(root,ref['file']))!=ref['sha256'] or ref['file'] not in manifest['files']:raise ValueError('target raw proof source closure differs')
    if cache.get('status')!='CACHE_RANGE_DMA_VERIFIED' or len(cache.get('cases',[]))!=3 or any(not x.get('passed') or not x.get('guard_unchanged') or not x.get('dirty_input_partial_flush') or not x.get('partial_output_invalidate') for x in cache['cases']):raise ValueError('real dirty-range VIEW_COPY cache qualification incomplete')
    if numeric.get('schema')!='pl01-target-numeric-v1' or numeric.get('status')!='PL01_TARGET_INTEGER_NUMERIC_PASS' or numeric.get('cache_proof_sha256')!=sha(root/CACHE_REPORT):raise ValueError('new PL01 target numeric proof incomplete')
    cases=numeric.get('cases',[])
    if len(cases)!=3 or {x.get('image') for x in cases}!={'2402','2493','2410'}:raise ValueError('three real target numeric probes required')
    for case in cases:
        error=case.get('probability_max_abs',math.inf)
        if case.get('native_batch_executed') is not True or case.get('conv_layers')!=79 or case.get('integer_mismatches')!=0 or not math.isfinite(error) or error>=1e-4:raise ValueError('new target integer/head numeric comparison failed')
    if not {CACHE_REPORT,PL_REPORT}<=set(manifest['files']):raise ValueError('target reports absent from production source identity')
    return dict(cache_proof_sha256=sha(root/CACHE_REPORT),pl_numeric_sha256=sha(root/PL_REPORT))

def verify_full(root=ROOT):
    root=Path(root).resolve();result=verify_sources(root);manifest=json.loads((root/MANIFEST).read_text())
    hardware=verify_camera_overlay(root);package,files=_offline(root)
    closure=set(files)|{p.relative_to(root).as_posix() for p in camera_module(root).bound_files(root)}
    if not closure<=set(manifest['files']):raise ValueError('production hardware/model source closure is incomplete')
    target=_verify_target(root,hardware,package,manifest)
    result.update(target,camera_overlay=hardware,package_manifest_sha256=package,bitstream_sha256=hardware['bitstream_sha256'],hwh_sha256=hardware['hwh_sha256'],
        pl_neural_network='FASTEST_W8PC_A16',production_qualified=True,physical_acceptance='NOT_ESTABLISHED_BY_AUTOMATED_SOURCE_NUMERIC_OR_CACHE_GATES')
    return result

def verify(root=ROOT,require_numeric=True):
    if require_numeric is not True:raise ValueError('no production numeric bypass; explicit verify_diagnostic is diagnostic-only')
    return verify_full(root)

def bound_files(root=ROOT):
    root=Path(root).resolve();record=json.loads((root/MANIFEST).read_text())
    return sorted(set([local_path(root,n) for n in record['files']]+[root/MANIFEST]+list(camera_module(root).bound_files(root))))
