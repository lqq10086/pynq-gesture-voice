"""Bind actual C-sim, HLS synthesis and exported IP; no board or build launch."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):return {'file':path.relative_to(ROOT).as_posix(),'sha256':sha(path)}


def main():
    sources={n:sha(ROOT/n) for n in ('hls/fastest_accel.cpp','hls/fastest_accel.h')}
    csim_path=ROOT/'reports/hls_full96_csim_summary.json'
    csim=json.loads(csim_path.read_text())
    assert csim['status']=='FULL96_INTEGER_CSIM_PASS_NOT_RTL_NOT_BOARD'
    assert csim['operations']==96 and csim['convolutions']==79 and csim['source_sha256']==sources
    log=ROOT/csim['full_graph_log']['file']
    assert sha(log)==csim['full_graph_log']['sha256']
    text=log.read_text(errors='replace')
    assert [int(i) for i in re.findall(r'GRAPH_OP_PASS index(\d+)',text)]==list(range(96))
    assert 'PL01_FULL96_HLS_CSIM_PASS ops96 conv79' in text and 'CSim done with 0 errors' in text
    xml_path=ROOT/'hls/build_csim/solution1/syn/report/fastest_accel_csynth.xml'
    xml=ET.parse(xml_path).getroot()
    get=xml.findtext
    part=get('UserAssignments/Part')
    assert get('UserAssignments/TopModelName')=='fastest_accel' and part.replace('-','')=='xc7z020clg4002'
    target=float(get('UserAssignments/TargetClockPeriod'))
    uncertainty=float(get('UserAssignments/ClockUncertainty'))
    estimated=float(get('PerformanceEstimates/SummaryOfTimingAnalysis/EstimatedClockPeriod'))
    assert target==10 and uncertainty==2.7 and estimated<=target
    resources={k:int(get('AreaEstimates/Resources/'+k)) for k in ('DSP','LUT','FF','BRAM_18K')}
    video={'DSP':15,'BRAM_18K':49,'LUT':11726,'FF':18575}
    assert resources['DSP']<=205 and resources['BRAM_18K']<=223 and resources['LUT']<=40000 and resources['FF']+video['FF']<=106400
    ip=ROOT/'hls/build_csim/solution1/impl/ip'
    component=ip/'component.xml';document=ET.parse(component).getroot()
    assert document.findtext('{*}name')=='fastest_accel' and document.findtext('{*}version')=='1.0'
    rtl={p.relative_to(ROOT).as_posix():sha(p) for p in sorted(ip.rglob('*')) if p.is_file() and p.suffix.lower() in ('.v','.sv','.vhd','.vhdl')}
    assert rtl
    synth_log=ROOT/'reports/hls_synth.log';text=synth_log.read_text(errors='replace')
    assert 'PL01_HLS_SYNTHESIS_COMPLETE_NOT_ROUTED_NOT_BOARD' in text and 'ERROR: [' not in text
    loops={name:{'requested_II':requested,'achieved_II':int(ii),'depth':int(depth)} for requested,ii,depth,name in
           re.findall(r"Target II = (\w+), Final II = (\d+), Depth = (\d+), loop '([^']+)'",text)}
    for name in ('LOAD_INPUT','LOAD_WEIGHTS','SHARED_MAC','REQUANT','STORE_PACKED'):
        assert loops[name]['achieved_II']==1
    record={'schema':'pl01-hls-synthesis-qualification-v1',
        'status':'HLS_RESOURCE_CLOCK_PASS_NOT_ROUTED_NOT_BOARD','top':'fastest_accel','part':part,
        'architecture':{'output_lanes':8,'dense_spatial_lanes':4,'dense_MACs':32,'true_DW_channel_lanes':8,
                        'shared_input_compute_requant_store':True,'accumulator_bits':48,'bias_checked_before_narrowing':True},
        'source_sha256':sources,'clock_target_ns':target,'clock_uncertainty_ns':uncertainty,
        'estimated_clock_ns':estimated,'bare_target_pass':estimated<=target,
        'uncertainty_budget_pass':estimated<=target-uncertainty,
        'clock_status':'WARNING_REQUIRES_ROUTED_TIMING' if estimated>target-uncertainty else 'HLS_ESTIMATE_WITHIN_UNCERTAINTY_NOT_ROUTED',
        'resources':resources,'video_reserved':video,
        'whole_design_estimate_without_new_interconnect':{k:resources[k]+v for k,v in video.items()},
        'loop_pipelines':loops,'artifacts':{'csynth_report':reference(xml_path.with_suffix('.rpt')),
        'csynth_xml':reference(xml_path),'synthesis_log':reference(synth_log),
        'export_log':reference(ROOT/'reports/hls_export.log'),
        'exported_ip':{'directory':ip.relative_to(ROOT).as_posix(),'component_xml':reference(component),'rtl_sha256':rtl}},
        'csim':{'status':'PASS','full96_plan':True,'report':reference(csim_path)},
        'interfaces':[n.findtext('{*}name') for n in document.findall('.//{*}busInterface')],
        'phase_ports':[n.findtext('{*}name') for n in document.findall('.//{*}port') if 'phase' in (n.findtext('{*}name') or '')],
        'latency_status':'NOT_MEASURED','routed_timing_status':'NOT_RUN','board_status':'NOT_RUN'}
    path=ROOT/'reports/hls_synthesis_qualification.json'
    path.write_text(json.dumps(record,indent=2)+'\n')
    print(json.dumps({'qualification':reference(path),'resources':resources,'estimated_clock_ns':estimated,
                      'clock_status':record['clock_status'],'interfaces':record['interfaces'],
                      'phase_ports':record['phase_ports'],'exported_rtl_files':len(rtl)},indent=2))


if __name__=='__main__':main()
