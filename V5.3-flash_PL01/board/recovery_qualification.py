"""Portable provenance proof for the one retained 435f97f3 route recovery."""
import copy,json
from pathlib import Path
from hardware_scripts.build_gate import Evidence,BASELINE_CDC,digest
from hardware_scripts.route_recovery import (ORIGIN,ADMISSION_SHA,FAILED_SHA,CHECKPOINT,CHECKPOINT_SHA,
    PROJECT,HWH,ARCHIVE,ALLOWED,NEW_HELPERS,COPIED,REASON,verify_original_files)


def verify_recovered_origin(root,report,result,admission):
    """Recheck every source/provenance byte locally; never read host absolute paths."""
    root=Path(root).resolve();report=Path(report).resolve();e=Evidence(root)
    if root/'reports' not in report.parents or report==root/ORIGIN:raise ValueError('Recovery must use a separate local report')
    old=json.loads(e.read(ORIGIN+'/prerequisite_admission.json',ADMISSION_SHA))
    failed=json.loads(e.read(ORIGIN+'/build_result.json',FAILED_SHA))
    if old.get('status')!='BUILD_READY_NOT_RUN' or len(old['bound_sha256'])!=315 or failed.get('status')!='FAILED_NOT_PUBLISHED_PASS':
        raise ValueError('Exact original315 failed build/admission required')
    record=admission.get('recovery')
    if not isinstance(record,dict):raise ValueError('recovery origin admission is missing')
    if (record.get('origin_report')!=ORIGIN or record.get('origin_admission_sha256')!=ADMISSION_SHA or
        record.get('origin_failed_result_sha256')!=FAILED_SHA or record.get('original_bound_file_count')!=315 or
        record.get('reason')!=REASON or record.get('route_rerun') is not False or record.get('synthesis_rerun') is not False or
        record.get('optimization_rerun') is not False or record.get('board')!='NOT_RUN' or
        result.get('origin_report')!=ORIGIN or result.get('recovered_reason')!=REASON):raise ValueError('recovery origin metadata differs')
    replacements=record.get('helper_replacements',{})
    if set(replacements)!=set(ALLOWED) or {name:old['bound_sha256'].get(name) for name in ALLOWED}!=ALLOWED:
        raise ValueError('Recovery must contain exactly two original helper replacements')
    for name,change in replacements.items():
        if change.get('archive')!=ARCHIVE+name:raise ValueError('Exact original helper archive path required')
    verify_original_files(root,old['bound_sha256'],replacements)
    helpers=record.get('recovery_helper_sha256',{})
    if set(helpers)!=set(NEW_HELPERS):raise ValueError('Recovery helper source closure differs')
    for name,value in helpers.items():e.read(name,value)
    expected_bound=dict(old['bound_sha256'])
    expected_bound.update({name:change['sha256'] for name,change in replacements.items()});expected_bound.update(helpers)
    expected_admission=copy.deepcopy(old);expected_admission['bound_sha256']=expected_bound;expected_admission['recovery']=record
    if admission!=expected_admission:raise ValueError('Recovery admission differs from exact original315 plus two replacements/new helpers')
    if record.get('checkpoint')!=dict(file=CHECKPOINT,sha256=CHECKPOINT_SHA):raise ValueError('Original routed checkpoint identity differs')
    e.read(CHECKPOINT,CHECKPOINT_SHA)
    retained=record.get('retained_project_sha256',{})
    if set(retained)!={PROJECT,HWH}:raise ValueError('Retained project/HWH provenance closure differs')
    for name,value in retained.items():e.read(name,value)
    copied=record.get('copied_artifacts_sha256',{})
    if set(copied)!=set(COPIED):raise ValueError('Original six report artifacts are required')
    report_name=report.relative_to(root).as_posix()
    for name,value in copied.items():e.read(ORIGIN+'/'+name,value);e.read(report_name+'/'+name,value)
    if (copied['baseline_cdc_unwaived.rpt']!=BASELINE_CDC or old['context']['lineage']['sha256']!=BASELINE_CDC):
        raise ValueError('Copied CDC baseline differs from original qualified SHA')
    if json.loads(e.read(report_name+'/recovery_origin.json'))!=record:raise ValueError('Saved recovery origin metadata differs')
    # Current315 + new helpers already appear in the selection's source closure.
    extra=[ORIGIN+'/prerequisite_admission.json',ORIGIN+'/build_result.json',CHECKPOINT,PROJECT,HWH,
        report_name+'/recovery_origin.json']+[ARCHIVE+name for name in ALLOWED]+[ORIGIN+'/'+name for name in COPIED]+[report_name+'/'+name for name in COPIED]
    return [str(e.path(name)) for name in extra]+[str(e.path('board/recovery_qualification.py'))]
