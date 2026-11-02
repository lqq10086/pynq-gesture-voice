"""Gated PL01 full build. Never resets another project or downloads hardware."""
import argparse,datetime,json,subprocess,sys,uuid
from pathlib import Path
from hardware_scripts.build_gate import check_prerequisites,digest
from hardware_scripts.route_gate import check_route

ROOT=Path(__file__).resolve().parent
VIVADO=Path('D:/Tools/FPGA/Vivado/2023.1/bin/vivado.bat')


def write_json(path,value):path.write_text(json.dumps(value,indent=2)+'\n',encoding='utf-8')


def unchanged(root,admission):
    for name,value in admission['bound_sha256'].items():
        if digest((root/name).read_bytes())!=value:raise ValueError('Prerequisite changed during build: '+name)
    baseline=Path(admission['context']['lineage']['file'])
    if digest(baseline.read_bytes())!=admission['context']['lineage']['sha256']:raise ValueError('Baseline CDC proof changed during build')


def bound_baseline(report,admission):
    baseline=report/'baseline_cdc_unwaived.rpt'
    if digest(baseline.read_bytes())!=admission['context']['lineage']['sha256']:
        raise ValueError('Copied baseline CDC differs from its admitted SHA')
    return baseline


def verify_route(root,report):
    root=Path(root).resolve();report=Path(report).resolve()
    if root/'reports' not in report.parents:raise ValueError('Routed output must be inside this project reports')
    admission=json.loads((report/'prerequisite_admission.json').read_text())
    if admission['status']!='BUILD_READY_NOT_RUN':raise ValueError('Build was not admitted')
    if 'recovery' in admission:
        from hardware_scripts.route_recovery import verify_recovery_origin
        verify_recovery_origin(root,report,admission)
    unchanged(root,admission)
    if check_prerequisites(root)['status']!='BUILD_READY_NOT_RUN':raise ValueError('Current prerequisites no longer pass')
    routed=check_route(report,bound_baseline(report,admission))
    saved=json.loads((report/'route_admission.json').read_text())
    if saved!=routed:raise ValueError('Routed evidence changed after admission')
    return routed


def run_vivado(root,report,script,args,label):
    command=[str(VIVADO),'-mode','batch','-nojournal','-log',str(report/(label+'.vivado.log')),
             '-source',str(script),'-tclargs']+[str(value) for value in args]
    # On Windows .bat launch is delegated to the normal cmd interpreter.
    with (report/(label+'.console.log')).open('x',encoding='utf-8') as output:
        process=subprocess.run(command,cwd=root/'hardware_project',stdout=output,stderr=subprocess.STDOUT,text=True)
    if process.returncode:raise RuntimeError(label+' failed; evidence retained in '+str(report))


def run_build(root=ROOT,check_only=False):
    root=Path(root).resolve();admission=check_prerequisites(root)
    if check_only:return admission
    stamp=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]
    report=root/'reports'/('hardware_build_'+stamp);report.mkdir(parents=True,exist_ok=False)
    write_json(report/'prerequisite_admission.json',admission)
    result=dict(status='BLOCKED',report=str(report),board_validation='NOT_RUN',errors=admission['errors'])
    if admission['status']!='BUILD_READY_NOT_RUN':
        write_json(report/'build_result.json',result);return result
    try:
        if not VIVADO.is_file():raise FileNotFoundError('Vivado 2023.1 executable missing: '+str(VIVADO))
        project=root/'hardware_project/hardware_build/vivado_flash_pl01/flash_pl01.xpr'
        if root not in project.resolve().parents:raise ValueError('Computed Vivado project path escapes this isolated root')
        if project.exists():raise FileExistsError('Existing new-root project retained; no automatic reset: '+str(project))
        for name in ('build_hardware.py','hardware_scripts/build_gate.py','hardware_scripts/route_gate.py',
                     'hardware_project/scripts/build_hardware_flash_pl01.tcl','hardware_project/scripts/publish_hardware_flash_pl01.tcl',
                     'hardware_project/scripts/report_v33_camera_paths.tcl'):
            admission['bound_sha256'][name]=digest((root/name).read_bytes())
        write_json(report/'prerequisite_admission.json',admission)
        baseline=Path(admission['context']['lineage']['file'])
        (report/'baseline_cdc_unwaived.rpt').write_bytes(baseline.read_bytes())
        run_vivado(root,report,root/'hardware_project/scripts/build_hardware_flash_pl01.tcl',
                   [report,root/admission['context']['hls']['ip_repo']],'route')
        unchanged(root,admission)
        routed=check_route(report,bound_baseline(report,admission))
        write_json(report/'route_admission.json',routed)
        verify_route(root,report)
        run_vivado(root,report,root/'hardware_project/scripts/publish_hardware_flash_pl01.tcl',[report,sys.executable],'publish')
        verify_route(root,report)
        bit=report/'flash_pl01.bit'
        if not bit.is_file() or not bit.stat().st_size:raise ValueError('No newly generated bit after routed gates')
        result.update(status='ROUTED_HARDWARE_BUILD_PASS_NOT_BOARD',errors=[],
            warnings=admission['warnings'],route=routed,
            files={name:digest((report/name).read_bytes()) for name in ('flash_pl01.bit','flash_pl01.hwh','final_routed.dcp')})
    except Exception as error:result.update(status='FAILED_NOT_PUBLISHED_PASS',errors=[repr(error)])
    write_json(report/'build_result.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check-only',action='store_true')
    parser.add_argument('--verify-route',type=Path,help=argparse.SUPPRESS);args=parser.parse_args()
    result=verify_route(ROOT,args.verify_route) if args.verify_route else run_build(check_only=args.check_only)
    display=dict(result)
    if args.check_only:display['bound_files']=len(display.pop('bound_sha256'))
    print(json.dumps(display,indent=2))
    return 0 if result['status'] in ('BUILD_READY_NOT_RUN','ROUTED_HARDWARE_BUILD_PASS_NOT_BOARD','ROUTED_CHECKS_PASS_NOT_BOARD') else 2


if __name__=='__main__':raise SystemExit(main())
