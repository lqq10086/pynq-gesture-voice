"""Portable pre-board route/model selection gate; never a board numeric PASS."""
import hashlib
import json
from pathlib import Path,PurePosixPath

SELECTION='reports/pl01_hardware_selection.json'


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def local(root,name):
    root=Path(root).resolve();relative=PurePosixPath(name);path=(root/name).resolve()
    if relative.is_absolute() or '..' in relative.parts or '\\' in name or relative.as_posix()!=name or root not in path.parents:raise ValueError('noncanonical PL01 reference: '+str(name))
    return path


def reference(root,record):
    path=local(root,record['file'])
    if sha(path)!=record['sha256']:raise ValueError('PL01 reference SHA differs: '+record['file'])
    return path


def verify_build_route(root,report,result,admission):
    status=result.get('status')
    if result.get('board_validation')!='NOT_RUN':raise ValueError('selection must remain NOT_RUN for board validation')
    if status=='RECOVERED_ROUTED_HARDWARE_BUILD_PASS_NOT_BOARD':
        if not isinstance(admission.get('recovery'),dict):raise ValueError('recovery origin admission is missing')
        from board.recovery_qualification import verify_recovered_origin
        return verify_recovered_origin(root,report,result,admission)
    if status!='ROUTED_HARDWARE_BUILD_PASS_NOT_BOARD':raise ValueError('selection is not a successful routed build')
    if 'recovery' in admission:raise ValueError('recovery admission cannot use ordinary fresh-build status')
    return []


def verify_host_selection(root):
    """Host sealer must establish strict full recovery verification before selection."""
    root=Path(root).resolve();selected=json.loads((root/SELECTION).read_text())
    report=reference(root,selected['build_result']).parent
    from build_hardware import verify_route
    verify_route(root,report)
    return verify_selection(root)


def verify_selection(root):
    root=Path(root).resolve();path=root/SELECTION
    if not path.is_file():raise ValueError('PL01 hardware selection is absent; diagnostic and production remain NOT_QUALIFIED')
    selected=json.loads(path.read_text())
    if selected.get('schema')!='pl01-hardware-selection-v1' or selected.get('status')!='ROUTED_HARDWARE_SELECTED_NOT_BOARD':raise ValueError('fresh routed PL01 selection required')
    refs={name:reference(root,selected[name]) for name in ('build_result','bitstream','hwh','dcp','hls_qualification')}
    result=json.loads(refs['build_result'].read_text())
    report=refs['build_result'].parent
    for name,key in [('bitstream','flash_pl01.bit'),('hwh','flash_pl01.hwh'),('dcp','final_routed.dcp')]:
        if refs[name]!=report/key or result['files'].get(key)!=selected[name]['sha256']:raise ValueError('selected artifact differs from actual build_result: '+name)
    admission=json.loads((report/'prerequisite_admission.json').read_text())
    if admission.get('status')!='BUILD_READY_NOT_RUN':raise ValueError('build was not admitted')
    recovery_bound=verify_build_route(root,report,result,admission)
    for name,value in admission['bound_sha256'].items():
        if sha(local(root,name))!=value:raise ValueError('admitted source/artifact changed: '+name)
    if admission['bound_sha256'].get(selected['hls_qualification']['file'])!=selected['hls_qualification']['sha256']:raise ValueError('HLS selection is not the admitted new IP')
    from hardware_scripts.build_gate import BASELINE_CDC,validate_hls
    from hardware_scripts.route_gate import check_route
    baseline=report/'baseline_cdc_unwaived.rpt'
    if sha(baseline)!=BASELINE_CDC or admission['context']['lineage']['sha256']!=BASELINE_CDC:raise ValueError('routed CDC baseline differs')
    routed=check_route(report,baseline)
    if routed!=result['route'] or routed!=json.loads((report/'route_admission.json').read_text()):raise ValueError('raw routed evidence no longer matches admission/result')
    hls_record=json.loads(refs['hls_qualification'].read_text());validate_hls(root,hls_record)
    controls=[name for name in hls_record['artifacts']['exported_ip']['rtl_sha256'] if Path(name).name=='fastest_accel_control_s_axi.v']
    if len(controls)!=1:raise ValueError('new exported IP needs one authoritative control register RTL')
    from board.register_map import parse_registers,assert_compatible
    frozen_map=json.loads((root/'board/register_map.json').read_text())
    assert_compatible(frozen_map,parse_registers(local(root,controls[0])))
    return dict(schema=selected['schema'],scope='PL01_ROUTED_STATIC_NOT_BOARD_NUMERIC',selection_sha256=sha(path),
        bitfile_name=str(refs['bitstream']),hwh_path=str(refs['hwh']),bitstream_sha256=selected['bitstream']['sha256'],hwh_sha256=selected['hwh']['sha256'],
        bound_files=sorted(set([str(path)]+[str(p) for p in refs.values()]+[str(local(root,n)) for n in admission['bound_sha256']]+[str(report/n) for n in routed['artifact_sha256']]+[str(report/'prerequisite_admission.json'),str(report/'route_admission.json')]+recovery_bound)))
