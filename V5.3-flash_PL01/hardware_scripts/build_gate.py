"""Read-only prerequisites and post-route checks; no Vivado/board invocation."""
from collections import Counter
import hashlib,json,math,re
from pathlib import Path,PurePosixPath
import xml.etree.ElementTree as ET

HLS_STATUS='HLS_RESOURCE_CLOCK_PASS_NOT_ROUTED_NOT_BOARD'
MODEL='e210ef35bdd9ea8e6f622cda6eba92ef5ef4b2d80371bdc204d39914d23bb394'
VIDEO={'DSP':15,'BRAM_18K':49,'LUT':11726,'FF':18575}
DEVICE={'DSP':220,'BRAM_18K':280,'LUT':53200,'FF':106400}
BASELINE_CDC='32327b3983620e0ded13c3db6ea28efac7954d17c5d9d859e4aaa953f1077903'
SUPPORT_SOURCES={'scripts/ps_config_ax7020_official_2023_1.tcl':'1c5d71cbda919256d857d4bfaffa822062a8a77a42129400c53f4974b4ddc9bc'}


def digest(raw):return hashlib.sha256(raw).hexdigest()


class Evidence:
    def __init__(self,root):self.root=Path(root).resolve();self.files={}
    def path(self,name):
        p=PurePosixPath(name);target=(self.root/name).resolve()
        if p.is_absolute() or '..' in p.parts or '\\' in name or p.as_posix()!=name or self.root not in target.parents:
            raise ValueError('Noncanonical project evidence path: '+name)
        return target
    def read(self,name,expected=None):
        path=self.path(name)
        if not path.is_file():raise ValueError('Missing prerequisite: '+name)
        raw=path.read_bytes();actual=digest(raw)
        if expected is not None and actual!=expected:raise ValueError('Evidence SHA differs: '+name)
        self.files[name]=actual;return raw
    def json(self,name):return json.loads(self.read(name))
    def reference(self,ref):return self.read(ref['file'],ref['sha256'])


def verify_lineage(e):
    record=e.json('reports/hardware_source_lineage.json');frozen=record['frozen_sources']
    if (len(frozen)!=65 or record.get('source_bit_sha256')!='4f65cb62178d2d384416db45c7b02e2c89878e8b575ab851cbabc216e59325c6' or
        record.get('source_hwh_sha256')!='37b819761b89828729e9208f688f0bd75369b27545658621ebfe32afe8c39cc6'):
        raise ValueError('Expected exact 65-source qualified camera lineage')
    actual={p.relative_to(e.root/'hardware_project').as_posix() for p in (e.root/'hardware_project/hardware').rglob('*') if p.is_file()}
    if actual!=set(frozen):raise ValueError('Prepared camera source inventory changed')
    for name,value in frozen.items():e.read('hardware_project/'+name,value)
    if record.get('support_sources')!=SUPPORT_SOURCES:
        raise ValueError('Lineage support_sources must contain exactly the qualified PS configuration script')
    for name,value in SUPPORT_SOURCES.items():e.read('hardware_project/'+name,value)
    created=e.read('hardware_project/scripts/create_hardware_flash_pl01.tcl').decode()
    if digest(created.replace('\r\n','\n').encode())!=record['new_create_sha256']:
        raise ValueError('Prepared create script changed beyond the qualified attach hook')
    hook=e.read('hardware_scripts/attach_fastest.tcl')
    if hook!=e.read('hardware_project/scripts/attach_fastest.tcl'):raise ValueError('Prepared attach hook differs')
    baseline=Path(record['source_root']).parent/'reports/camera_reference/cdc_unwaived.rpt'
    if not baseline.is_file() or digest(baseline.read_bytes())!=BASELINE_CDC:
        raise ValueError('Qualified baseline unwaived CDC evidence is missing or changed')
    return {'file':str(baseline),'sha256':BASELINE_CDC}


def verify_quant(e):
    package=e.json('models/quant_2020/manifest.json');package_sha=e.files['models/quant_2020/manifest.json']
    if package.get('model_sha256')!=MODEL:raise ValueError('Quant package model differs')
    for name,value in package['files'].items():e.read('models/quant_2020/'+name,value['sha256'])
    calibration=e.json('reports/quant_calibration_2020.json')
    if (calibration.get('status')!='PASS' or calibration.get('count')!=2020 or calibration.get('validation_observed')!=0 or
        calibration.get('package_manifest_sha256')!=package_sha):raise ValueError('Full train-only 2020 calibration is required')
    result=e.json('reports/quant_eval_2020/summary.json')
    if (result.get('status')!='OFFLINE_ACCURACY_GATE_PASS' or result.get('images')!=507 or result.get('observer_validation_overlap')!=0 or
        result.get('package_manifest_sha256')!=package_sha or result.get('model_sha256')!=MODEL):
        raise ValueError('Current 2020-package complete 507-image accuracy gate must pass')
    for name,value in result['source_sha256'].items():e.read(name,value)
    domains=result['comparisons']
    if domains['FPGA']['images']!=133 or domains['legacy']['images']!=374:raise ValueError('Both validation domains are required')
    if domains['FPGA']['ap50_drop']>.01 or any(v['recall_drop']>.02 for v in domains['FPGA']['per_class'].values()):
        raise ValueError('Per-domain/per-class fixed-threshold accuracy gate failed')
    return package_sha


def verify_physical(e,package_sha):
    result=e.json('reports/physical_binary_replay/summary.json')
    if (result.get('status')!='PHYSICAL_PLAN_HOST_REPLAY_PASS' or result.get('operation_count')!=96 or
        result.get('package_manifest_sha256')!=package_sha or 'packed' not in result.get('scope','').lower()):
        raise ValueError('Three-probe independent packed-binary replay is required')
    e.read('reports/physical_bundle_2402/plan/plan.json',result['plan_sha256'])
    bundle=e.json('reports/physical_bundle_2402/manifest.json')
    if bundle.get('package_manifest_sha256')!=package_sha:raise ValueError('Physical bundle package differs')
    for name,value in bundle['files'].items():e.read('reports/physical_bundle_2402/'+name,value['sha256'])
    paths={'replay_plan.py':'compiler/replay_plan.py','physical_plan.py':'compiler/physical_plan.py','integer_reference.py':'host/integer_reference.py'}
    for name,value in result['source_sha256'].items():e.read(paths[name],value)
    probes=result['probes']
    if len(probes)!=3 or {p['image'] for p in probes}!={'2402','2493','2410'}:raise ValueError('Expected exact three physical replay probes')
    for probe in probes:
        if (probe.get('status')!='PASS' or probe.get('conv_layers')!=79 or probe.get('integer_mismatches')!=0 or
            len(probe.get('layer_comparisons',[]))!=79 or any(v['mismatches'] for v in probe['layer_comparisons']) or
            {v.get('conv_index') for v in probe['layer_comparisons']}!=set(range(79)) or
            set(probe.get('head_max_abs',{}))!={'head_s16','head_s32'} or
            any(not math.isfinite(v) or v>=1e-4 for v in probe['head_max_abs'].values())):
            raise ValueError('Physical packed-binary replay layer/head mismatch: '+probe['image'])


def validate_hls(root,record,evidence=None):
    e=evidence or Evidence(root)
    if record.get('schema')!='pl01-hls-synthesis-qualification-v1' or record.get('status')!=HLS_STATUS:
        raise ValueError('Missing '+HLS_STATUS+' qualification; generic PASS cannot authorize an IP')
    if record.get('top')!='fastest_accel' or record.get('part','').replace('-','')!='xc7z020clg4002':
        raise ValueError('HLS top/part differs')
    sources=record['source_sha256']
    if not {'hls/fastest_accel.cpp','hls/fastest_accel.h'}<=set(sources):raise ValueError('HLS current source binding incomplete')
    for name,value in sources.items():e.read(name,value)
    target=float(record['clock_target_ns']);estimated=float(record['estimated_clock_ns']);uncertainty=float(record['clock_uncertainty_ns'])
    if target!=10 or uncertainty!=2.7 or not math.isfinite(estimated) or estimated>target:
        raise ValueError('HLS bare 10ns target failed or original uncertainty changed')
    bare=estimated<=target;margin=estimated<=target-uncertainty
    if record.get('bare_target_pass') is not bare or record.get('uncertainty_budget_pass') is not margin:
        raise ValueError('HLS clock booleans differ from numeric estimates')
    if record.get('video_reserved')!=VIDEO:raise ValueError('HLS video resource reservation differs')
    resources=record['resources']
    for name,limit in DEVICE.items():
        if int(resources[name])+VIDEO[name]>limit:raise ValueError('HLS plus video exceeds device '+name)
    artifacts=record['artifacts'];e.reference(artifacts['csynth_report'])
    xml=ET.fromstring(e.reference(artifacts['csynth_xml']))
    if (xml.findtext('UserAssignments/TopModelName')!='fastest_accel' or
        xml.findtext('UserAssignments/Part','').replace('-','')!='xc7z020clg4002' or
        float(xml.findtext('UserAssignments/TargetClockPeriod'))!=target or
        float(xml.findtext('UserAssignments/ClockUncertainty'))!=uncertainty):
        raise ValueError('Raw HLS XML top/part/clock assignment differs')
    for name in DEVICE:
        if int(xml.findtext('AreaEstimates/Resources/'+name))!=int(resources[name]):raise ValueError('HLS resource report differs: '+name)
    if abs(float(xml.findtext('PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod'))-estimated)>1e-6:
        raise ValueError('HLS estimated clock differs from raw XML')
    csim=record['csim']
    if csim.get('status')!='PASS' or csim.get('full96_plan') is not True:raise ValueError('Full 96-operation C-sim pass is required')
    e.reference(csim['report'])
    ip=artifacts['exported_ip'];directory=e.path(ip['directory']);component=ET.fromstring(e.reference(ip['component_xml']))
    if e.path(ip['component_xml']['file']).parent!=directory:raise ValueError('IP component is outside its declared export directory')
    if component.findtext('{*}name')!='fastest_accel' or component.findtext('{*}version')!='1.0':raise ValueError('Exported IP component differs')
    rtl=ip['rtl_sha256'];actual={p.relative_to(e.root).as_posix() for p in directory.rglob('*') if p.is_file() and p.suffix.lower() in ('.v','.sv','.vhd','.vhdl')}
    if not actual or actual!=set(rtl):raise ValueError('Exported IP RTL hash closure differs')
    for name,value in rtl.items():e.read(name,value)
    return dict(ip_repo=ip['directory'],bare_target_pass=bare,uncertainty_budget_pass=margin,
        warnings=[] if margin else ['WARNING_REQUIRES_ROUTED_TIMING'])


def check_prerequisites(root):
    e=Evidence(root);errors=[];context={}
    for name,function in (('lineage',lambda:verify_lineage(e)),('quant',lambda:verify_quant(e)),
                          ('hls',lambda:validate_hls(e.root,e.json('reports/hls_synthesis_qualification.json'),e))):
        try:context[name]=function()
        except (ValueError,KeyError,TypeError,OSError,ET.ParseError) as error:errors.append(name+': '+str(error))
    if 'quant' in context:
        try:verify_physical(e,context['quant'])
        except (ValueError,KeyError,TypeError,OSError) as error:errors.append('physical: '+str(error))
    return dict(status='BLOCKED' if errors else 'BUILD_READY_NOT_RUN',errors=errors,
        warnings=context.get('hls',{}).get('warnings',[]),context=context,bound_sha256=e.files,
        scope='PREREQUISITES_ONLY_NO_VIVADO_NO_BOARD')


def cdc_rows(text):
    source=destination=None;rows=[]
    for line in text.splitlines():
        if line.startswith('Source Clock:'):source=line.split(':',1)[1].strip()
        elif line.startswith('Destination Clock:'):destination=line.split(':',1)[1].strip()
        elif re.match(r'^\s*\d+\s+CDC-\d+\s+(Critical|Warning)\s',line):
            fields=re.split(r'\s{2,}',line.strip())
            if len(fields)!=8 or source is None or destination is None:raise ValueError('Unparsed unsafe CDC endpoint: '+line)
            rows.append(tuple([source,destination]+fields[1:]))
    return Counter(rows)


def compare_cdc(baseline,current):
    old=cdc_rows(baseline);new=cdc_rows(current)
    if not old:raise ValueError('Baseline unsafe CDC endpoint set was not parsed')
    if old!=new:raise ValueError('CDC Critical/Warning endpoint multiset differs: added='+str(list((new-old).elements()))+' removed='+str(list((old-new).elements())))
    return dict(status='EXACT_BASELINE_UNSAFE_ENDPOINTS',endpoints=sum(old.values()),
        waiver_policy='NO_NEW_CRITICAL_OR_WARNING_ENDPOINTS_NO_BLANKET_WAIVERS')
