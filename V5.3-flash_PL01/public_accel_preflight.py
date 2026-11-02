"""Static Q1 artifact admission, never a substitute for target qualification.

This validates pinned Q1 bit/HWH, routed reports and inference parameters.
When the checkpoint is absent it is explicitly marked as not checked. Static
checks alone cannot claim a DMA/numeric pass or production board qualification.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

MODEL = 'e210ef35bdd9ea8e6f622cda6eba92ef5ef4b2d80371bdc204d39914d23bb394'
REPORT = 'reports/hardware_recovery_435f97f3_20261026_170641_ead3b9fa'
EXPECTED = {
    'models/gesture_best.onnx': MODEL,
    'models/quant_2020/manifest.json': '4fc4047ff5206944b53f63efb2b947b9cb958acc378375991951dbd567b8c2a1',
    'models/quant_2020/weights_padded.bin': 'ebfb3f7599010a1485733d88cce5bb1000bc07055dd523cea9d3afdd81a737dc',
    'models/quant_2020/bias.bin': 'b501fd7d5d9f5d9af7dea5dd9de4e31ee9a99927d473482f1ac31867cdecb6bb',
    'models/quant_2020/shift.bin': 'ba962ed144cd79f857dec3160ef9ce783cbe61332233b5e1c40766b494460db4',
    'reports/physical_bundle_2402/plan/manifest.json': '757413df5707c097adecc8cf48c179d651330844db530434f09b6599748be355',
    'reports/physical_bundle_2402/plan/plan.json': 'a00d927d6be33b798b73167973cf7c58a6d92d7b6247c08a63c608b9255e5465',
    'reports/physical_bundle_2402/plan/channel_offsets.bin': '925a0e90e6eef277253138afa0d0930b680da49a61364eec2ee453540f7e15b6',
    'reports/physical_bundle_2402/plan/channel_shifts.bin': 'e315dfe9a555c35a10f5d6413f0a1c6c102811fb710d053ca596abd6068518b5',
    REPORT + '/flash_pl01.bit': '7c6de24f663c9dd94ad1e51ff05382cc53fcfa773327f6c4dd052a08dcc8ce75',
    REPORT + '/flash_pl01.hwh': 'd6ef66b5102eec3dc2942ebe8b9e629d3db0639ea4a10bbd19559298e9e4ba2c',
    REPORT + '/build_result.json': '3094ef4ea95ab9621049618308ac0f0919f2890a21c75ba89718868a80985c49',
    REPORT + '/baseline_cdc_unwaived.rpt': '32327b3983620e0ded13c3db6ea28efac7954d17c5d9d859e4aaa953f1077903',
    'reports/hls_synthesis_qualification.json': '3ad9bf73a38dcb40f2b67152f61b455a6ec6d4d149bf79371ba38c941027e39d',
    'board/pl_runner.py': '58fcd6fe24842f17275057c9ff8a62ddd67873d9432210b6fa438f455c608a2e',
    'board/pl_adapter.py': '9934eb38fedab18f60d79f615bd55bd4f049227b0e23ed9b62bd0a29a18b523b',
    'board/owned_hardware.py': '79c118528a8143f26a385465a1f91619cd15aaa59744e58dc0af1c1146b35109',
    'board/cache_qualification.py': 'ecb1e3f9a16519ef002cfb49df0a800d1a700b523d5672eb42ec6149151ff616',
    'board/register_map.json': 'a2b470a883d6bb57e9a0dd8fa769758871935cb8784fc571ef88fb2c86dbff95',
    'board/build_pl_batch/armv7/libpl_batch.so': 'f7a4dbc92d6d26d1e2c4b3a5cbd978c8cda6445f92dc062d7353bb3322145761',
    'hardware_driver.py': 'ac9e83aa6f3be07dabc1abe95a3f37b447db672a770db86f617e803bc9898cdd',
    'board_entry.py': '414ea6b4df298bd2e5877f42a1b0124862cb454136c54ce8ca7cda43ffffaa6d',
    'native/build_v2/armv7/libfused_preprocess_v2.so': '48cd3ed8d431149aaffd4021f8140c6adf9f0b6414db0eb38d4d8c805b2cf9b8',
    'board/nn_only_overlay.py': '506bc431b4dfdee8ddbe53c869b9085e307b2cfc8395f58eab46c96292a4305e',
    'backend_v51/runtime/owner_guard.py': 'e79abe7008bb6211f967020b6878006bc3ce2be8839d6271cb781ed4b3aa17f1',
    'backend_v51/runtime/camera_control.py': 'd34fa57fd905edaf8a774a1fe09a7bf4232961bdf57d22ebcafd404d55425767',
    'backend_v51/runtime/hardware_idle.py': '894e1d7c2b15c2eac8dab8377a96b9fa158ed298f0abb4b4e7ed66df0d30f695',
    'ui_plane_output.py': 'b8ebe21742e3f530699b76eb9fa006d09daf0661d14073304315f4ea835dbf13',
    'hardware_scripts/build_gate.py': '8f6f24e1cd998002ff9e81ea2106ae3bb89cc671353a09c53885150b46d078df',
    'hardware_scripts/route_gate.py': '9f2980ab54fc0df38c5d443a068ea00afbb2844991dc9abf536097846cd2129a',
    'camera_qualification.py': 'f6102b4555b7065ec69a4b69966d3ee759d83ec05f02dbc3e2f6fa251b5bd9fa',
    'dual_overlay.py': '893f09308ec8c826631168a5da9e537d05e59641603b3d9c52d163adeb1087f5',
    'board/pl_batch.py': 'a6f99ebe8b73652d549f5f103afe9553672d280496b3bf10741a1d23177f1989',
    'board/register_map.py': 'e2b9e25358c4f51d09f67f13418bc096bdd0c954292b3197af7448e014fe2fec',
    'compiler/replay_plan.py': '6ef2acee5768812870430b056d617049be21156861a175233440a16d135ec8b0',
    'host/integer_reference.py': 'f2141bd9e07af85195e4918d4321d2c600f9e6f435f5fea96853ac176e63fecc',
    'reports/pl01_hardware_selection.json': '7a9c9378e5fd88acceb93c002a3e2dc48262c84d8f7a113eca3a25d749b346c1',
    'backend_v51/public_accel_runtime_identity.json': '881acc2ffc004bc28b4b810980cc7812e5361916da2bf47710400be53810179b',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_files(root, hashes):
    root = Path(root).resolve()
    for name, expected in hashes.items():
        rel = PurePosixPath(name)
        path = (root / name).resolve()
        if (rel.is_absolute() or '..' in rel.parts or '\\' in name or
                rel.as_posix() != name or root not in path.parents):
            raise ValueError('nonlocal artifact path: ' + name)
        if not path.is_file() or digest(path) != expected:
            raise ValueError('artifact identity changed: ' + name)


def bound_routed_files(root):
    # The route file list is accepted only from the pinned build result.
    root = Path(root).resolve()
    build_name = REPORT + '/build_result.json'
    verify_files(root, {build_name: EXPECTED[build_name]})
    route = json.loads((root / build_name).read_text())['route']['artifact_sha256']
    result = []
    for name in route:
        relative = PurePosixPath(name)
        if relative.is_absolute() or len(relative.parts) != 1 or name in ('.', '..') or relative.as_posix() != name or '\\' in name:
            raise ValueError('invalid routed artifact name: ' + str(name))
        path = (root / REPORT / name).resolve()
        if (root / REPORT).resolve() not in path.parents:
            raise ValueError('routed artifact escapes report directory: ' + name)
        if name != 'final_routed.dcp' or path.exists() or (root / REPORT / name).is_symlink():
            result.append(path)
    return result


def verify_static(root):
    root = Path(root).resolve()
    verify_files(root, EXPECTED)
    package = json.loads((root / 'models/quant_2020/manifest.json').read_text())
    plan = json.loads((root / 'reports/physical_bundle_2402/plan/plan.json').read_text())
    if (package.get('model_sha256') != MODEL or plan.get('model_sha256') != MODEL or
            plan.get('package_manifest_sha256') != EXPECTED['models/quant_2020/manifest.json'] or
            plan.get('operation_count') != 96 or plan.get('operation_counts') != {'0':79, '1':1, '2':1, '3':15}):
        raise ValueError('sanitized model or operation plan differs')
    report = root / REPORT
    build = json.loads((report / 'build_result.json').read_text())
    if (build.get('status') != 'RECOVERED_ROUTED_HARDWARE_BUILD_PASS_NOT_BOARD' or
            build.get('board_validation') != 'NOT_RUN' or
            build['files']['flash_pl01.bit'] != EXPECTED[REPORT + '/flash_pl01.bit'] or
            build['files']['flash_pl01.hwh'] != EXPECTED[REPORT + '/flash_pl01.hwh']):
        raise ValueError('routed artifact origin differs')
    from hardware_scripts.route_gate import check_route
    route = check_route(report, report / 'baseline_cdc_unwaived.rpt', allow_missing_dcp=True)
    expected_route = build['route']
    if (expected_route.get('status') != 'ROUTED_CHECKS_PASS_NOT_BOARD' or
            expected_route.get('artifact_sha256', {}).get('final_routed.dcp') != build.get('files', {}).get('final_routed.dcp')):
        raise ValueError('routed checkpoint origin differs')
    if route['status'] == 'ROUTED_REPORTS_CHECKED_DCP_NOT_CHECKED_NOT_BOARD':
        expected_route = dict(expected_route)
        expected_route['status'] = route['status']
        expected_route['artifact_sha256'] = dict(expected_route['artifact_sha256'])
        del expected_route['artifact_sha256']['final_routed.dcp']
        dcp_validation, route_validation = 'NOT_INCLUDED', 'REPORTS_CHECKED_DCP_NOT_CHECKED'
    else:
        dcp_validation, route_validation = 'SHA256_VERIFIED', 'REPORTS_AND_DCP_CHECKED'
    if route != expected_route:
        raise ValueError('raw routed checks differ')
    from hardware_scripts.build_gate import validate_hls
    hls = validate_hls(root, json.loads((root / 'reports/hls_synthesis_qualification.json').read_text()))
    from camera_qualification import camera_projection, verify_dual_hwh
    hwh = (report / 'flash_pl01.hwh').read_bytes()
    baseline = (root / 'reports/camera_reference/baseline_hwh.hwh').read_bytes()
    if hashlib.sha256(baseline).hexdigest() != 'e33d31fe8eb0cbc1c5756ef9d70a3dc01cb5c8845788276280efab2146974549':
        raise ValueError('camera baseline identity differs')
    if camera_projection(hwh) != camera_projection(baseline):
        raise ValueError('camera/video endpoint configuration differs')
    verify_dual_hwh(hwh)
    tree = ET.fromstring(hwh)
    modules = tree.findall('.//MODULE')
    neural = [m for m in modules if m.get('VLNV', '').split(':')[-2:-1] == ['fastest_accel']]
    if len(neural) != 1 or neural[0].get('INSTANCE') != 'fastest_accel_0':
        raise ValueError('Fastest accelerator identity differs')
    endpoints = {}
    for prefix, name, vlnv, address in (
            ('ip', 'fastest_accel_0', 'xilinx.com:hls:fastest_accel:1.0', 0x43c00000),
            ('phase', 'flash_phase_gpio', 'xilinx.com:ip:axi_gpio:2.0', 0x412d0000)):
        found = [m for m in modules if m.get('INSTANCE') == name and m.get('VLNV') == vlnv]
        ranges = [m for m in tree.findall('.//MEMRANGE') if m.get('INSTANCE') == name]
        if (len(found) != 1 or len(ranges) != 1 or int(ranges[0].get('BASEVALUE'), 0) != address or
                int(ranges[0].get('HIGHVALUE'), 0) != address + 65535):
            raise ValueError('accelerator MMIO identity differs: ' + name)
        endpoints.update({prefix + '_instance':name, prefix + '_type':vlnv,
                          prefix + '_base_addr':address, prefix + '_addr_range':65536})
    camera = dict(scope='CAMERA_WB_DUAL_OVERLAY_AND_FASTEST_STATIC',
        bitfile_name=str(report / 'flash_pl01.bit'), hwh_path=str(report / 'flash_pl01.hwh'),
        bitstream_sha256=EXPECTED[REPORT + '/flash_pl01.bit'], hwh_sha256=EXPECTED[REPORT + '/flash_pl01.hwh'],
        selection_sha256=EXPECTED['reports/pl01_hardware_selection.json'],
        bound_files=sorted(set([str(root / name) for name in EXPECTED] + [str(path) for path in bound_routed_files(root)])), **endpoints)
    return dict(status='STATIC_ONLY_NOT_BOARD_QUALIFIED', board_qualified=False,
                operation_count=96, model_sha256=MODEL, bitstream_sha256=EXPECTED[REPORT + '/flash_pl01.bit'],
                hwh_sha256=EXPECTED[REPORT + '/flash_pl01.hwh'], dcp_validation=dcp_validation,
                route_validation=route_validation, hls_warnings=hls['warnings'], camera_overlay=camera)
