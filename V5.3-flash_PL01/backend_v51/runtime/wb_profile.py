"""Apply only a previously accepted WB profile after its formal owner starts.

No configs/sensor_wb_profile.json means pure SKIPPED_RO, including no MMIO attach.
Profiles bind qualified bit/HWH and exact accepted parameters, not an old PID or
boot. Every application obtains fresh owner identity and a new original-state
journal below that owner's output. This module never derives gains from pixels.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import time
import uuid
import xml.etree.ElementTree as ET

try:
    from . import sensor_wb
except ImportError:
    import sensor_wb

PARAMETER_KEYS = ('mode','controls','gains','bitstream_sha256','hwh_sha256')
PROFILE_KEYS = set(PARAMETER_KEYS) | {'status','validation_file','validation_sha256'}
CONTROL_MASKS = {'5001':1,'5196':0x20,'3406':1}
REBIND_FROM = {
    'bitstream_sha256':'28bc4eda5ddebbc6486681066d3b0766dec9aff299d8e3816b97291f22201c2c',
    'hwh_sha256':'99e0ee132765042c9130126e717d8051a20eca5aa9c9f0d515c2eb36a87559a0'}
REBIND_TO = {
    'bitstream_sha256':'b4605e38fc999af654d0773b7fb063be6c42a2727a93f69fc3f91a6a5721788e',
    'hwh_sha256':'b3127b019515d343b2ee891e6d3653532901c1c15eff11468e343d6b56d16ee0'}
REBIND_PROVENANCE = 'reports/v51_hardware_reuse.json'
REBIND_PROVENANCE_SHA256 = 'c9b3a4cff49f6b2b77442029017b1a41c75f3cde7c8a796558d8cf220a285441'
ORIGINAL_ACCEPTANCE = 'reports/wb1_color_accepted_20261018.json'
ORIGINAL_ACCEPTANCE_SHA256 = 'b236e79cd81222af288315a854e36c3d530116c746dc96f8a5566b0f0972a1a7'
CAMERA_WB_REBIND_SCHEMA = 'v52-camera-wb-only-rebind-v1'
CAMERA_WB_BASELINE_MANIFEST_SHA256 = '15331814f07cd93a78b61495dd01adbb2686be26082fff1fea3e13522488ad35'
CAMERA_WB_WRAPPER_SHA256 = '6c0272c5b546109266716f1b47c1ada93b24eaf0e59a042099ac1bafdb4b996e'
CAMERA_WB_COMPILED_NAMES = (
    'system_top.v', 'camera_config_v02.v', 'camera_control_axi.v', 'camera_control_bridge.v',
    'camera_dvp_to_axis_rgb565.v', 'clk_gen.v', 'color_mode_control.v', 'color_status_cdc.v',
    'fpga_color_pipeline.v', 'fpga_spatial_chroma_correction.v', 'ov5640_af.v',
    'pl_color_commit.v', 'pl_color_frame_control.v', 'sccb_write.v', 'sensor_wb_bridge.v',
    'sensor_wb_control.v', 's2mm_write_observer.v', 'stats_snapshot_cdc.v', 'video_stats_snapshot.v')


class WbProfileError(RuntimeError):
    def __init__(self,message,*,journal=None):
        super().__init__(message)
        self.journal = str(journal) if journal is not None else None


def _report_reference(root,reference):
    if not isinstance(reference['file'],str) or not reference['file']:
        raise ValueError('Camera/WB evidence path is empty or invalid')
    relative = PurePosixPath(reference['file'])
    if (not isinstance(reference['file'],str) or '\\' in reference['file']
            or relative.is_absolute() or '..' in relative.parts
            or relative.as_posix() != reference['file'] or relative.parts[0] != 'reports'):
        raise ValueError('Camera/WB evidence must use a canonical reports path')
    path = (root/relative).resolve()
    if root/'reports' not in path.parents:
        raise ValueError('Camera/WB evidence resolves outside reports')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise ValueError('Camera/WB evidence SHA-256 differs: '+reference['file'])
    return raw


def _compiled_inventory(raw):
    headers,files = {},{}
    for line in raw.decode('utf-8-sig').splitlines():
        cells = line.split('\t')
        kind = cells[0]
        if kind in ('DCP_SHA256','INSPECTOR_SHA256','PROJECT'):
            if kind in headers or len(cells) != (3 if kind == 'PROJECT' else 2):
                raise ValueError('Invalid or duplicate compiled inventory header')
            headers[kind] = cells[1:]
        elif kind in ('COMPILED','PROJECT_FILE') and len(cells) == 4:
            key = (kind,cells[1])
            if key in files and files[key] != cells[2]:
                raise ValueError('Conflicting duplicate compiled source inventory entry')
            files[key] = cells[2]
        else:
            raise ValueError('Unrecognized compiled source inventory row')
    if set(headers) != {'DCP_SHA256','INSPECTOR_SHA256','PROJECT'} or not files:
        raise ValueError('Incomplete actual-project compiled inventory')
    return headers,files


def _camera_hwh_identity(raw):
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError('Invalid camera/WB HWH') from error
    try:
        from .camera_control import EXPECTED_IP, EXPECTED_VDMA_PARAMETERS
    except ImportError:
        from camera_control import EXPECTED_IP, EXPECTED_VDMA_PARAMETERS
    identity = {}
    for name,(address,size,vlnv) in EXPECTED_IP.items():
        modules = [m for m in tree.findall('.//MODULE') if m.get('INSTANCE') == name]
        ranges = [(int(m.get('BASEVALUE'),0),int(m.get('HIGHVALUE'),0))
                  for m in tree.findall('.//MEMRANGE') if m.get('INSTANCE') == name]
        if len(modules) != 1 or modules[0].get('VLNV') != vlnv or ranges != [(address,address+size-1)]:
            raise ValueError('Camera/WB HWH endpoint identity differs: '+name)
        module = modules[0]
        parameters = {p.get('NAME'):p.get('VALUE') for p in module.findall('./PARAMETERS/PARAMETER')}
        if name == 'axi_vdma_0' and any(parameters.get(k) != v for k,v in EXPECTED_VDMA_PARAMETERS.items()):
            raise ValueError('Camera/WB HWH VDMA configuration differs')
        identity[name] = dict(vlnv=vlnv,range=ranges,parameters=parameters,
            registers=[(r.get('NAME'),r.get('ADDRESS_OFFSET'),r.get('SIZE')) for r in module.findall('.//REGISTER')])
    ps = [m for m in tree.findall('.//MODULE') if m.get('INSTANCE') == 'processing_system7_0']
    if len(ps) != 1: raise ValueError('Camera/WB HWH requires one PS clock source')
    ps_parameters = {p.get('NAME'):p.get('VALUE') for p in ps[0].findall('./PARAMETERS/PARAMETER')}
    if any(ps_parameters.get(k) != v for k,v in (('C_USE_S_AXI_HP0','1'),('C_S_AXI_HP0_DATA_WIDTH','64'))):
        raise ValueError('Camera/WB HWH HP0 configuration differs')
    hp0 = [(m.get('MASTERBUSINTERFACE'),int(m.get('BASEVALUE'),0),int(m.get('HIGHVALUE'),0))
           for m in tree.findall('.//MEMRANGE') if m.get('INSTANCE') == 'processing_system7_0'
           and m.get('SLAVEBUSINTERFACE') == 'S_AXI_HP0'
           and m.get('MASTERBUSINTERFACE') in ('M_AXI_MM2S','M_AXI_S2MM')]
    if sorted(hp0) != [('M_AXI_MM2S',0,0x3fffffff),('M_AXI_S2MM',0,0x3fffffff)]:
        raise ValueError('Camera/WB HWH HP0 address window differs')
    clocks = {p.get('NAME'):int(p.get('CLKFREQUENCY')) for p in ps[0].findall('./PORTS/PORT')
              if p.get('NAME') in ('FCLK_CLK0','FCLK_CLK1') and p.get('CLKFREQUENCY')}
    if (set(clocks) != {'FCLK_CLK0','FCLK_CLK1'} or clocks['FCLK_CLK0'] != 100000000
            or abs(clocks['FCLK_CLK1']-142857143) > 100):
        raise ValueError('Camera/WB HWH clocks differ')
    identity['clocks'] = clocks
    identity['hp0'] = sorted(hp0)
    return identity


def _camera_wb_chain_evidence(root,provenance,parameters):
    expected_keys = {'schema','scope','accelerator_changed','human_acceptance','chain_baseline_hardware',
                     'to_hardware','baseline_manifest','baseline_hwh','build_manifest','checkpoint_sha256',
                     'compiled_inventory','inspector','project','sources','generated_bd_sources'}
    if (set(provenance) != expected_keys or provenance['schema'] != CAMERA_WB_REBIND_SCHEMA
            or provenance['scope'] != 'CAMERA_WB_CHAIN_ONLY' or provenance['accelerator_changed'] is not True
            or provenance['human_acceptance'] != dict(file=ORIGINAL_ACCEPTANCE,
                sha256=ORIGINAL_ACCEPTANCE_SHA256,hardware=REBIND_FROM)
            or provenance['chain_baseline_hardware'] != REBIND_TO
            or provenance['to_hardware'] != {k:parameters[k] for k in REBIND_TO}
            or provenance['to_hardware'] == REBIND_TO):
        raise ValueError('Unapproved camera/WB-only rebind scope or hardware lineage')
    for name in ('baseline_manifest','baseline_hwh','build_manifest','compiled_inventory','inspector','project'):
        if set(provenance[name]) != {'file','sha256'}:
            raise ValueError('Unexpected camera/WB evidence reference fields')
    baseline_raw = _report_reference(root,provenance['baseline_manifest'])
    if hashlib.sha256(baseline_raw).hexdigest() != CAMERA_WB_BASELINE_MANIFEST_SHA256:
        raise ValueError('Original camera/WB source manifest changed')
    baseline = json.loads(baseline_raw)['files']
    build = json.loads(_report_reference(root,provenance['build_manifest']))['files']
    baseline_hwh = _report_reference(root,provenance['baseline_hwh'])
    if hashlib.sha256(baseline_hwh).hexdigest() != REBIND_TO['hwh_sha256']:
        raise ValueError('Compiled-chain baseline HWH changed')
    for suffix,key in (('.bit','bitstream_sha256'),('.hwh','hwh_sha256')):
        raw = (root/'runtime/hardware'/('camera_v32_384'+suffix)).read_bytes()
        if hashlib.sha256(raw).hexdigest() != parameters[key]:
            raise ValueError('Camera/WB rebind target artifact changed: '+key)
        if suffix == '.hwh' and _camera_hwh_identity(raw) != _camera_hwh_identity(baseline_hwh):
            raise ValueError('Camera/WB HWH endpoint projection changed')
    headers,inventory = _compiled_inventory(_report_reference(root,provenance['compiled_inventory']))
    inspector = _report_reference(root,provenance['inspector'])
    project = _report_reference(root,provenance['project'])
    if (headers['DCP_SHA256'] != [provenance['checkpoint_sha256']]
            or not re.fullmatch('[0-9a-f]{64}',provenance['checkpoint_sha256'])
            or headers['INSPECTOR_SHA256'] != [hashlib.sha256(inspector).hexdigest()]
            or headers['PROJECT'][1] != hashlib.sha256(project).hexdigest()
            or b'get_files -compile_order sources -used_in synthesis' not in inspector
            or b'get_files -all' not in inspector):
        raise ValueError('Actual compiled inventory DCP, inspector or project binding differs')
    required = {'hardware/rtl/'+name for name in CAMERA_WB_COMPILED_NAMES}
    required.update(name for name in baseline if name.startswith('hardware/rtl/') and name.endswith(('.mem','.vh')))
    seen = set()
    for source in provenance['sources']:
        if set(source) != {'logical_path','file','sha256','inventory_path','inventory_kind'}:
            raise ValueError('Unexpected camera/WB source evidence fields')
        name = source['logical_path']
        if name in seen: raise ValueError('Duplicate camera/WB source snapshot')
        seen.add(name)
        _report_reference(root,source)
        if (baseline.get(name) != source['sha256'] or build.get(name) != source['sha256']
                or not source['inventory_path'].replace('\\','/').endswith('/'+name)
                or inventory.get((source['inventory_kind'],source['inventory_path'])) != source['sha256']
                or (name in required and name.endswith('.v') and source['inventory_kind'] != 'COMPILED')):
            raise ValueError('Camera/WB source was changed or not actually compiled: '+name)
    if not required <= seen: raise ValueError('Required compiled camera/WB sources are missing')
    generated = provenance['generated_bd_sources']
    if len(generated) != 1: raise ValueError('One generated camera control BD source is required')
    wrapper = generated[0]
    if (set(wrapper) != {'file','sha256','inventory_path','inventory_kind'}
            or wrapper['sha256'] != CAMERA_WB_WRAPPER_SHA256
            or not wrapper['inventory_path'].endswith('/system_camera_control_0_0/synth/system_camera_control_0_0.v')
            or inventory.get((wrapper['inventory_kind'],wrapper['inventory_path'])) != wrapper['sha256']):
        raise ValueError('Generated camera control BD source differs from compiled baseline')
    _report_reference(root,wrapper)


def _camera_wb_rebound_acceptance(root,profile,parameters):
    binding = profile['hardware_rebind']
    expected = dict(schema=CAMERA_WB_REBIND_SCHEMA,from_hardware=REBIND_FROM,
        to_hardware={k:parameters[k] for k in REBIND_TO},
        provenance_file=binding['provenance_file'],provenance_sha256=binding['provenance_sha256'])
    if (binding != expected or profile['validation_file'] != ORIGINAL_ACCEPTANCE
            or profile['validation_sha256'] != ORIGINAL_ACCEPTANCE_SHA256):
        raise ValueError('Unapproved camera/WB rebind or human acceptance identity')
    reference = dict(file=binding['provenance_file'],sha256=binding['provenance_sha256'])
    raw = _report_reference(root,reference)
    record = json.loads((root/'runtime/runtime_qualification.json').read_bytes())
    if (record.get('sources',{}).get(binding['provenance_file']) != binding['provenance_sha256']
            or any(record.get('fixed',{}).get('runtime/hardware/camera_v32_384'+suffix) != parameters[key]
                   for suffix,key in (('.bit','bitstream_sha256'),('.hwh','hwh_sha256')))):
        raise ValueError('Camera/WB rebind lacks final runtime qualification authorization')
    _camera_wb_chain_evidence(root,json.loads(raw),parameters)
    return {**parameters,**REBIND_FROM}


def _rebound_acceptance(root,profile,parameters):
    """Authorize only this exact hardware reuse, without changing acceptance."""
    if not isinstance(profile['hardware_rebind'],dict):
        raise ValueError('WB hardware rebind must be an object')
    if profile['hardware_rebind'].get('schema') == CAMERA_WB_REBIND_SCHEMA:
        return _camera_wb_rebound_acceptance(root,profile,parameters)
    expected = dict(schema='v51-wb-hardware-rebind-v1',from_hardware=REBIND_FROM,
                    to_hardware=REBIND_TO,provenance_file=REBIND_PROVENANCE,
                    provenance_sha256=REBIND_PROVENANCE_SHA256)
    if (profile['hardware_rebind'] != expected
            or any(parameters[key] != value for key,value in REBIND_TO.items())
            or profile['validation_file'] != ORIGINAL_ACCEPTANCE
            or profile['validation_sha256'] != ORIGINAL_ACCEPTANCE_SHA256):
        raise ValueError('Unapproved WB hardware rebind or original acceptance identity')
    evidence = root/REBIND_PROVENANCE
    raw = evidence.read_bytes()
    if hashlib.sha256(raw).hexdigest() != REBIND_PROVENANCE_SHA256:
        raise ValueError('WB hardware rebind provenance SHA-256 differs')
    provenance = json.loads(raw)
    if (provenance.get('schema') != 'v51-hardware-reuse-v1'
            or provenance.get('status') != 'HARDWARE_REUSE_PROVENANCE_NOT_FRESH_BOARD_ACCEPTANCE'
            or provenance.get('from_hardware') != REBIND_FROM
            or provenance.get('to_hardware') != REBIND_TO):
        raise ValueError('WB hardware rebind provenance has the wrong scope')
    for suffix,key in (('.bit','bitstream_sha256'),('.hwh','hwh_sha256')):
        target = root/'runtime/hardware'/('camera_v32_384'+suffix)
        if hashlib.sha256(target.read_bytes()).hexdigest() != REBIND_TO[key]:
            raise ValueError('WB rebind target artifact differs: '+key)
    # The original human acceptance binds the original hardware. Fresh owner,
    # candidate numeric acceptance and live readback are still required below.
    return {**parameters,**REBIND_FROM}


def _parameters(root,path):
    try:
        profile = json.loads(path.read_text())
        if (set(profile) not in (PROFILE_KEYS,PROFILE_KEYS|{'hardware_rebind'})
                or profile['status'] != 'ACCEPTED_WB_PROFILE'):
            raise ValueError('Profile is not an accepted WB profile with the required fields')
        mode = profile['mode']
        controls = profile['controls']
        if mode not in ('auto','manual') or controls != {'5001':1,'5196':0,'3406':int(mode=='manual')}:
            raise ValueError('Only AWB enabled, unfrozen and mode-matching WB controls are permitted')
        if any(type(v) is not int for v in controls.values()):
            raise ValueError('Control values must be integer target bits')
        gains = profile['gains']
        if mode == 'auto':
            if gains is not None: raise ValueError('Auto profile must use gains=null')
        elif (not isinstance(gains,list) or len(gains)!=3
              or any(type(g) is not int or not 0x100 <= g <= 0xfff for g in gains)):
            raise ValueError('Manual profile requires three integer gains 0x100..0xfff')
        for key in ('bitstream_sha256','hwh_sha256','validation_sha256'):
            if not isinstance(profile[key],str) or not re.fullmatch('[0-9a-f]{64}',profile[key]):
                raise ValueError('Invalid SHA-256 binding: '+key)
        relative = Path(profile['validation_file'])
        if relative.is_absolute() or not relative.parts or relative.parts[0] != 'reports':
            raise ValueError('Validation evidence must be a relative path below reports')
        evidence = (root/relative).resolve()
        if root/'reports' not in evidence.parents:
            raise ValueError('Resolved validation evidence escapes candidate reports')
        raw = evidence.read_bytes()
        if hashlib.sha256(raw).hexdigest() != profile['validation_sha256']:
            raise ValueError('Validation evidence SHA-256 differs')
        validation = json.loads(raw)
        parameters = {key:profile[key] for key in PARAMETER_KEYS}
        accepted = (_rebound_acceptance(root,profile,parameters)
                    if 'hardware_rebind' in profile else parameters)
        if (validation.get('status') != 'COLOR_WB_COMPARISON_ACCEPTED'
                or json.dumps(validation.get('accepted_profile'),sort_keys=True)
                   != json.dumps(accepted,sort_keys=True)):
            raise ValueError('Actual accepted validation parameters do not exactly match profile')
        return profile,parameters
    except (OSError,ValueError,TypeError,KeyError) as error:
        raise WbProfileError('WB profile rejected before any Sensor write: '+str(error)) from error


def _verify_final(snapshot,parameters):
    for key,mask in CONTROL_MASKS.items():
        if snapshot['controls'][key] & mask != parameters['controls'][key]:
            raise WbProfileError('Final WB control readback differs at '+key)
    if parameters['mode']=='manual' and snapshot['manual_gains'] != parameters['gains']:
        raise WbProfileError('Final manual gain readback differs from accepted profile')


def apply_accepted_profile(root,*,attach_factory=None,identity_check=None):
    """Return {status: SKIPPED_RO/APPLIED,...}; raise on any unverified result.

    Root startup must safely stop its newly started child when this raises.
    A restoration attempt restores original manual bytes and allowed control
    bits, not the Sensor's internal dynamic AWB history. Wait for auto convergence
    after restore. No production configuration is created by this function.
    """
    root = Path(root).resolve()
    path = root/'configs'/'sensor_wb_profile.json'
    if not path.exists(): return dict(status='SKIPPED_RO',reason='No accepted WB profile configured')
    profile,parameters = _parameters(root,path)
    proof = identity_check or sensor_wb.read_identity
    identity = proof(root)
    for key,field in [('bitstream','bitstream_sha256'),('hwh','hwh_sha256')]:
        if identity.get(key,{}).get('sha256') != parameters[field]:
            raise WbProfileError('Accepted profile hardware differs from current live '+key)
    output = Path(identity['output']).resolve()
    if identity.get('root') != str(root) or root/'reports' not in output.parents or not output.is_dir():
        raise WbProfileError('Profile application output is not the current candidate owner output')
    parent = output/'sensor_wb_startup'
    if parent.resolve() != parent:
        raise WbProfileError('Startup journal directory resolves outside the current owner output')
    parent.mkdir(exist_ok=True)
    folder = parent/(str(time.time_ns())+'_'+uuid.uuid4().hex[:8])
    folder.mkdir()
    journal = folder/'apply.json'
    attach = attach_factory or sensor_wb.attach
    record = None
    client = None
    first = None
    try:
        with attach(root,allow_write=True) as client:
            if client.identity != identity:
                raise WbProfileError('Live owner changed before accepted profile attachment')
            before = client.snapshot()
            record = dict(schema='accepted-wb-startup-v1',state='PREPARED',identity=identity,
                          profile=profile,pre=before,operation_journals=[],prepared_unix_s=time.time())
            sensor_wb._journal(journal,record,initial=True)
            try:
                operations = []
                if parameters['mode']=='manual':
                    operations.append(('set_gains',lambda p:client.set_gains(*parameters['gains'],p)))
                for key in ('5001','5196','3406'):
                    operations.append(('rmw_'+key,lambda p,key=key:client.rmw8(
                        int(key,16),CONTROL_MASKS[key],parameters['controls'][key],p)))
                for number,(name,operation) in enumerate(operations,1):
                    operation_path = folder/('%02d_%s.json'%(number,name))
                    if first is None: first = operation_path
                    record['operation_journals'].append(str(operation_path))
                    record['state'] = 'APPLYING'
                    sensor_wb._journal(journal,record)
                    operation(operation_path)
                record['post'] = client.snapshot()
                _verify_final(record['post'],parameters)
                record['state'] = 'VERIFIED_PENDING_CLIENT_CLOSE'
                sensor_wb._journal(journal,record)
            except Exception as error:
                restoration = dict(status='NOT_ATTEMPTED_NO_OPERATION_JOURNAL')
                if first is not None and first.is_file():
                    restore_path = folder/'restore_original.json'
                    try:
                        restored = client.restore(first,restore_path)
                        if restored.get('state') != 'COMPLETE':
                            raise WbProfileError('Restore did not return verified COMPLETE')
                        restoration = dict(status='RESTORED',journal=str(restore_path))
                    except Exception as restore_error:
                        restoration = dict(status='FAILED',journal=str(restore_path),error=str(restore_error))
                record.update(state='FAILED',error=str(error),restoration=restoration,completed_unix_s=time.time())
                sensor_wb._journal(journal,record)
                raise WbProfileError('Accepted WB application failed; startup must stop its child: '+str(error),
                                     journal=journal) from error
        record.update(state='COMPLETE',completed_unix_s=time.time())
        sensor_wb._journal(journal,record)
        return dict(status='APPLIED',journal=str(journal),mode=parameters['mode'],post=record['post'])
    except Exception as error:
        if record is not None and record['state'] != 'FAILED':
            record.update(state='FAILED',error=str(error),completed_unix_s=time.time(),
                          restoration=dict(status='NOT_ATTEMPTED_CLIENT_CLOSED_OR_UNCERTAIN'))
            sensor_wb._journal(journal,record)
        if isinstance(error,WbProfileError): raise
        raise WbProfileError('Accepted WB application failed; startup must stop its child: '+str(error),
                             journal=journal if record is not None else None) from error
