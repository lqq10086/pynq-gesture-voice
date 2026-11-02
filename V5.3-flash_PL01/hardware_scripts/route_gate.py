"""Validate routed files before any candidate bit is written."""
import math,re
from pathlib import Path
import xml.etree.ElementTree as ET
from .build_gate import compare_cdc,digest


def tsv(path):return dict(line.split('\t',1) for line in Path(path).read_text().splitlines() if '\t' in line)


def verify_hwh(path):
    tree=ET.parse(path).getroot();modules=tree.findall('.//MODULE')
    neural=[m for m in modules if 'fastest_accel' in m.get('VLNV','')]
    if len(neural)!=1 or neural[0].get('INSTANCE')!='fastest_accel_0' or not neural[0].get('VLNV','').endswith(':fastest_accel:1.0'):
        raise ValueError('HWH needs exactly one newly exported Fastest core')
    if any('conv_accel' in (m.get('INSTANCE','')+' '+m.get('VLNV','')) for m in modules):raise ValueError('HWH contains old conv_accel')
    for name,address in (('fastest_accel_0',0x43c00000),('flash_phase_gpio',0x412d0000),('bbox_shadow_gpio2',0x412b0000),('bbox_shadow_gpio3',0x412c0000)):
        ranges=[(int(r.get('BASEVALUE'),0),int(r.get('HIGHVALUE'),0)) for r in tree.findall('.//MEMRANGE') if r.get('INSTANCE')==name]
        if ranges!=[(address,address+65535)]:raise ValueError('HWH register range differs: '+name)
    ps=[m for m in modules if m.get('INSTANCE')=='processing_system7_0']
    if len(ps)!=1:raise ValueError('HWH needs one PS')
    params={p.get('NAME'):p.get('VALUE') for p in ps[0].findall('./PARAMETERS/PARAMETER')}
    if params.get('C_USE_S_AXI_HP1')!='1' or params.get('C_S_AXI_HP1_DATA_WIDTH')!='64':raise ValueError('HWH HP1 is not enabled 64-bit')


def check_route(report,baseline_cdc,*,allow_missing_dcp=False):
    report=Path(report)
    required=('facts.tsv','bd_facts.tsv','synthesis_core.tsv','routed_core.tsv','dual_bbox_routed.tsv',
        'timing_summary.rpt','pulse_width.rpt','bus_skew.rpt','utilization.rpt','drc.rpt','cdc_unwaived_view.rpt',
        'check_timing.rpt','camera_input_facts.tsv','camera_input_setup.rpt','camera_input_hold.rpt',
        'clocks.rpt','clock_interaction.rpt','exceptions_coverage.rpt','exceptions_ignored.rpt','route_status.rpt',
        'ip_status.rpt','compiled_inventory.rpt','final_routed.dcp','flash_pl01.hwh','route_stage_complete.txt','baseline_cdc_unwaived.rpt')
    dcp=report/'final_routed.dcp'
    missing_dcp=not dcp.exists() and not dcp.is_symlink()
    if allow_missing_dcp and missing_dcp:
        required=tuple(name for name in required if name!='final_routed.dcp')
    for name in required:
        if not (report/name).is_file() or not (report/name).stat().st_size:raise ValueError('Missing routed evidence: '+name)
    facts=tsv(report/'facts.tsv')
    if facts.get('ROUTED_FULLY') not in ('1','true'):raise ValueError('Design is not fully routed')
    for key in ('ERRORS_IN_ROUTES','PARTIALLY_ROUTED','setup_bad','hold_bad','drc_blocking','locked_ips'):
        if facts.get(key) not in ('0','false'):raise ValueError('Routed gate failed: '+key)
    bd=tsv(report/'bd_facts.tsv')
    for key,value in {'fastest_core_count':'1','old_conv_core_count':'0','hp1_enabled':'1','hp1_width':'64','control_MI':'14','new_hp1_clock':'FCLK0'}.items():
        if bd.get(key)!=value:raise ValueError('Fresh BD contract differs: '+key)
    for stage in ('synthesis','routed'):
        core=tsv(report/(stage+'_core.tsv'))
        if core.get('fastest_core_count')!='1' or core.get('old_conv_core_count')!='0':raise ValueError('Actual '+stage+' core count differs')
    dual=tsv(report/'dual_bbox_routed.tsv')
    for key in ('active0','active1','active2','active4','active5','active6','pending4','pending5','pending6','meta4','meta5','meta6','bbox_shadow_gpio2','bbox_shadow_gpio3'):
        if key not in dual:raise ValueError('Actual routed dual-slot state missing: '+key)
    for name in ('pulse_width.rpt','bus_skew.rpt'):
        if re.search(r'\bVIOLATED\b',(report/name).read_text()):raise ValueError('Routed pulse-width/bus-skew violation: '+name)
    counts={key:int(count) for key,count in re.findall(r'^\d+\. checking (\w+) \((\d+)\)',(report/'check_timing.rpt').read_text(),re.M)}
    expected={'no_clock':0,'constant_clock':0,'pulse_width_clock':0,'unconstrained_internal_endpoints':0,
        'no_input_delay':4,'no_output_delay':11,'multiple_clock':0,'generated_clocks':0,'loops':0,
        'partial_input_delay':0,'partial_output_delay':0,'latch_loops':0}
    if counts!=expected:raise ValueError('Clock/unconstrained check differs from preserved camera baseline: '+str(counts))
    camera=[]
    for line in (report/'camera_input_facts.tsv').read_text().splitlines():
        fields=line.split('\t')
        if fields and not fields[0].startswith('clock:'):
            if len(fields)!=3 or fields[1] not in ('max','min') or not math.isfinite(float(fields[2])) or float(fields[2])<0:
                raise ValueError('Camera input setup/hold path failed: '+line)
            camera.append((fields[0],fields[1]))
    if len(camera)!=20 or len(set(camera))!=20:raise ValueError('Expected all ten camera inputs with both setup/hold paths')
    cdc=compare_cdc(Path(baseline_cdc).read_text(),(report/'cdc_unwaived_view.rpt').read_text())
    verify_hwh(report/'flash_pl01.hwh')
    status='ROUTED_REPORTS_CHECKED_DCP_NOT_CHECKED_NOT_BOARD' if missing_dcp else 'ROUTED_CHECKS_PASS_NOT_BOARD'
    return dict(status=status,cdc=cdc,artifact_sha256={name:digest((report/name).read_bytes()) for name in required})
