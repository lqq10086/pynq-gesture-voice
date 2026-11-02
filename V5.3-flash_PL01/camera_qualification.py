"""New PL01 routed camera/UI/WB gate; static scope is separate from PL numeric."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import xml.etree.ElementTree as ET

BASELINE_BIT='b28294463632fd45a10b9bcba0704e6eb626c656960276b5cf204ae007e16ab1'
BASELINE_HWH='e33d31fe8eb0cbc1c5756ef9d70a3dc01cb5c8845788276280efab2146974549'
SCHEMA='v52-dual-overlay-camera-only-v1'
REPORT='reports/camera_dual_qualification.json'
PROFILE='backend_v51/configs/sensor_wb_profile.json'
ALLOWED_CHANGED={'hardware/rtl/bbox_overlay.v','hardware/rtl/system_top.v',
                 'hardware/constraints/ax7020_ddr_vdma_ov5640_hdmi.xdc',
                 'hardware/bd/system_bd.tcl','hardware/bd/ui_bd.tcl'}
REMOVED_AI={'hardware/bd/accel_bd.tcl','hardware/rtl/accel_phase_timer.v'}
ADDED_WRAPPER_LINES={
    'wire [31:0] bbox2_word0,bbox2_word1,bbox2_word2;',
    '.BBOX2_WORD0(bbox2_word0),.BBOX2_WORD1(bbox2_word1),.BBOX2_WORD2(bbox2_word2),',
    '.shadow4(bbox2_word0),.shadow5(bbox2_word1),.shadow6(bbox2_word2),',
}


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_reference(root,reference):
    name=reference['file'];relative=PurePosixPath(name)
    if (not isinstance(name,str) or '\\' in name or relative.is_absolute() or '..' in relative.parts or
            relative.as_posix()!=name or not relative.parts or relative.parts[0]!='reports'):
        raise ValueError('Camera proof requires a canonical path below reports')
    path=(Path(root)/name).resolve()
    if (Path(root).resolve()/'reports') not in path.parents:raise ValueError('Camera proof path escapes reports')
    raw=path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=reference['sha256']:raise ValueError('Camera proof SHA differs: '+name)
    return raw


def verify_wrapper(baseline,changed):
    lines=changed.splitlines(keepends=True)
    additions=[line.strip() for line in lines if line.strip() in ADDED_WRAPPER_LINES]
    if len(additions)!=3 or set(additions)!=ADDED_WRAPPER_LINES:
        raise ValueError('Wrapper must add exactly the three dual-slot wire connections')
    remaining=''.join(line for line in lines if line.strip() not in ADDED_WRAPPER_LINES)
    if remaining.replace('\r\n','\n')!=baseline.replace('\r\n','\n'):
        raise ValueError('Camera/UI/color wrapper changed beyond dual-slot connections')


def verify_inventory(baseline,changed):
    for name,value in baseline.items():
        if not name.startswith('hardware/'):
            # This proof preserves the camera/UI source chain. Neural HLS/IP
            # inputs and its old build scripts are deliberately not retained.
            continue
        if name in REMOVED_AI:
            if name in changed:raise ValueError('Old neural-only hardware source remains: '+name)
            continue
        if name not in ALLOWED_CHANGED and changed.get(name)!=value:
            raise ValueError('Preserved hardware source changed: '+name)


def verify_facts(raw):
    facts=dict(line.split('\t',1) for line in raw.splitlines() if '\t' in line)
    if facts.get('ROUTED_FULLY') not in ('1','true'):
        raise ValueError('New dual-slot design is not fully routed')
    for name in ('ERRORS_IN_ROUTES','PARTIALLY_ROUTED','setup_bad','hold_bad','drc_blocking','locked_ips'):
        if facts.get(name) not in ('0','false'):
            raise ValueError('New dual-slot route/timing/DRC gate failed: '+name)
    return facts


def camera_projection(raw):
    tree=ET.fromstring(raw);result={}
    for name in ('video_status_gpio','video_control_gpio','camera_control_0','axi_vdma_0',
                 'ui_vdma','ui_status_gpio0','ui_status_gpio1'):
        modules=[m for m in tree.findall('.//MODULE') if m.get('INSTANCE')==name]
        if len(modules)!=1:raise ValueError('Camera HWH endpoint count differs: '+name)
        module=modules[0]
        result[name]=dict(vlnv=module.get('VLNV'),
            parameters={p.get('NAME'):p.get('VALUE') for p in module.findall('./PARAMETERS/PARAMETER')},
            ranges=sorted((r.get('BASEVALUE'),r.get('HIGHVALUE')) for r in tree.findall('.//MEMRANGE') if r.get('INSTANCE')==name),
            registers=[(r.get('NAME'),r.get('ADDRESS_OFFSET'),r.get('SIZE')) for r in module.findall('.//REGISTER')])
    ps=[m for m in tree.findall('.//MODULE') if m.get('INSTANCE')=='processing_system7_0']
    if len(ps)!=1:raise ValueError('Camera HWH requires one PS clock source')
    params={p.get('NAME'):p.get('VALUE') for p in ps[0].findall('./PARAMETERS/PARAMETER')}
    result['ps']=dict(parameters={k:params.get(k) for k in ('C_USE_S_AXI_HP0','C_S_AXI_HP0_DATA_WIDTH','C_USE_S_AXI_HP2','C_S_AXI_HP2_DATA_WIDTH')},
        clocks={p.get('NAME'):p.get('CLKFREQUENCY') for p in ps[0].findall('./PORTS/PORT') if p.get('NAME') in ('FCLK_CLK0','FCLK_CLK1')},
        hp0=sorted((r.get('MASTERBUSINTERFACE'),r.get('BASEVALUE'),r.get('HIGHVALUE')) for r in tree.findall('.//MEMRANGE')
                   if r.get('INSTANCE')=='processing_system7_0' and r.get('SLAVEBUSINTERFACE') in ('S_AXI_HP0','S_AXI_HP2')))
    return result


def verify_dual_hwh(raw):
    tree=ET.fromstring(raw)
    from dual_overlay import GPIO_ADDRESSES
    for name,address in GPIO_ADDRESSES.items():
        modules=[m for m in tree.findall('.//MODULE') if m.get('INSTANCE')==name]
        ranges=[(int(r.get('BASEVALUE'),0),int(r.get('HIGHVALUE'),0)) for r in tree.findall('.//MEMRANGE') if r.get('INSTANCE')==name]
        if len(modules)!=1 or modules[0].get('VLNV')!='xilinx.com:ip:axi_gpio:2.0' or ranges!=[(address,address+65535)]:
            raise ValueError('New dual-slot HWH layout differs: '+name)
        if name!='bbox_ack_gpio':
            params={p.get('NAME'):p.get('VALUE') for p in modules[0].findall('./PARAMETERS/PARAMETER')}
            if any(params.get(k)!=v for k,v in (('C_IS_DUAL','1'),('C_GPIO_WIDTH','32'),('C_GPIO2_WIDTH','32'))):
                raise ValueError('Dual-slot shadow GPIO widths differ: '+name)


def _legacy_bound_files(root):
    root=Path(root).resolve();report=json.loads((root/REPORT).read_text())
    paths=[root/REPORT,root/PROFILE,root/'backend_v51/runtime/hardware/camera_v32_384.bit',
           root/'backend_v51/runtime/hardware/camera_v32_384.hwh']
    paths.extend(root/ref['file'] for ref in report['references'].values())
    return list(dict.fromkeys(paths))


def _verify_legacy_camera_only(root):
    root=Path(root).resolve();report=json.loads((root/REPORT).read_text())
    if (report.get('schema')!=SCHEMA or report.get('status')!='CAMERA_CHAIN_AND_DUAL_OVERLAY_VERIFIED' or
            report.get('neural_network_scope')!='NOT_PRESENT_CPU_ONLY' or
            report.get('baseline_hardware')!={'bitstream_sha256':BASELINE_BIT,'hwh_sha256':BASELINE_HWH}):
        raise ValueError('Fresh dual-slot camera-only qualification is absent or has wrong scope')
    refs={name:read_reference(root,reference) for name,reference in report['references'].items()}
    baseline=json.loads(refs['baseline_manifest'])['files'];changed=json.loads(refs['build_manifest'])['files']
    source_copy=json.loads(refs['source_copy'])
    if (source_copy.get('status')!='QUALIFIED_SOURCE_COPY_VERIFIED' or source_copy.get('mismatches') or
            source_copy.get('qualified_bit_sha256')!=BASELINE_BIT or source_copy.get('qualified_hwh_sha256')!=BASELINE_HWH or
            source_copy.get('baseline_source_manifest_sha256')!=hashlib.sha256(refs['baseline_manifest']).hexdigest()):
        raise ValueError('Dual overlay is not based on the exact qualified V5.2 hardware')
    verify_inventory(baseline,changed)
    verify_wrapper(refs['baseline_top'].decode(),refs['new_top'].decode())
    verify_facts(refs['routed_facts'].decode())
    review=json.loads(refs['hardware_review'])
    if (review.get('status')!='CAMERA_ONLY_STATIC_REVIEW_PASS_WITH_PRESERVED_CDC_SCOPES_BOARD_NOT_RUN' or
            review.get('resources',{}).get('used',{}).get('DSP')!=15 or
            review.get('neural_hwh_absence',{}).get('status')!='PASS'):
        raise ValueError('Lean routed camera/UI/dual-overlay review did not pass')
    if b'DUAL_BBOX_SIM_PASS checks=6 pixels=3072' not in refs['simulation']:
        raise ValueError('Dual-slot directed simulation proof is absent')
    bit=root/'backend_v51/runtime/hardware/camera_v32_384.bit'
    hwh=bit.with_suffix('.hwh');identity=dict(bitstream_sha256=digest(bit),hwh_sha256=digest(hwh))
    if identity!=report.get('hardware') or identity['bitstream_sha256']==BASELINE_BIT:
        raise ValueError('New dual-slot bit/HWH bytes differ from this qualification')
    if (review['artifacts'].get('camera_v32_384.bit')!=identity['bitstream_sha256'] or
            review['artifacts'].get('camera_v32_384.hwh')!=identity['hwh_sha256']):
        raise ValueError('New camera artifact differs from its actual routed review')
    if hashlib.sha256(refs['baseline_hwh']).hexdigest()!=BASELINE_HWH:
        raise ValueError('Qualified camera baseline HWH changed')
    raw=hwh.read_bytes()
    if any(m.get('INSTANCE')=='conv_accel_0' or 'conv_accel' in m.get('VLNV','') for m in ET.fromstring(raw).findall('.//MODULE')):
        raise ValueError('CPU-only V5.3-flash hardware must not contain the old neural accelerator')
    if camera_projection(raw)!=camera_projection(refs['baseline_hwh']):
        raise ValueError('Camera endpoint/clock/VDMA projection changed')
    verify_dual_hwh(raw)
    if (b'SOURCE\t' not in refs['compiled_inventory'] or b'bbox_overlay' not in refs['routed_dual_paths'] or
            b'bbox_shadow_gpio2' not in refs['routed_dual_paths'] or b'bbox_shadow_gpio3' not in refs['routed_dual_paths'] or
            b'NO_CONV_ACCEL_CELLS=1' not in refs['routed_dual_paths'] or
            not refs['routed_neural_absence'].startswith(b'neural_cells\t0')):
        raise ValueError('Routed design lacks actual dual-slot inventory/path evidence')
    return dict(identity,scope='CAMERA_WB_AND_DUAL_OVERLAY_ONLY',hardware_slots=2,
                physical_camera_acceptance='PENDING_LIVE_READBACK',qualification_sha256=digest(root/REPORT))


def _legacy_load_wb_profile(root):
    root=Path(root).resolve();identity=verify_camera_overlay(root)
    profile=json.loads((root/PROFILE).read_text())
    parameters={k:profile.get(k) for k in ('mode','controls','gains','bitstream_sha256','hwh_sha256')}
    if (profile.get('status')!='ACCEPTED_PARAMETERS_REBOUND_CAMERA_ONLY' or parameters['mode']!='manual' or
            parameters['controls']!={'5001':1,'5196':0,'3406':1} or parameters['gains']!=[1460,1024,1796] or
            any(parameters[k]!=identity[k] for k in ('bitstream_sha256','hwh_sha256')) or
            profile.get('qualification_sha256')!=digest(root/REPORT)):
        raise ValueError('New camera-only WB profile parameters or hardware binding differ')
    return profile,parameters


def verify_camera_overlay(root):
    from public_accel_preflight import verify_static
    return verify_static(Path(root).resolve())['camera_overlay']


def bound_files(root):
    root=Path(root).resolve();identity=verify_camera_overlay(root)
    report=json.loads((root/REPORT).read_text())
    return [Path(p) for p in identity['bound_files']]+[root/REPORT,root/PROFILE]+[root/x['file'] for x in report['references'].values()]


def load_wb_profile(root):
    root=Path(root).resolve();identity=verify_camera_overlay(root);original=json.loads((root/PROFILE).read_text())
    if original.get('mode')!='manual' or original.get('gains')!=[1460,1024,1796] or original.get('controls')!={'5001':1,'5196':0,'3406':1}:
        raise ValueError('preserved accepted manual WB parameters differ')
    profile=dict(original,status='ACCEPTED_PARAMETERS_REBOUND_PL01_STATIC',bitstream_sha256=identity['bitstream_sha256'],hwh_sha256=identity['hwh_sha256'],
        selection_sha256=identity['selection_sha256'],original_profile_sha256=digest(root/PROFILE))
    parameters={k:profile[k] for k in ('mode','controls','gains','bitstream_sha256','hwh_sha256')}
    return profile,parameters
