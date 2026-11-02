"""Read fresh full-route evidence; never approve using an old DCP's exceptions."""
import hashlib
import json
import re
import sys
from pathlib import Path
import review_hardware_384_D as base
import review_hardware_v33 as v33
from check_ov5640_clock import audit

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT/'reports/hardware/activation_ii1'
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    policy = json.loads((ROOT/'reference/hardware_reviewed_video_exceptions_v33.json').read_text())
    result = dict(status='FAIL', deployment_allowed=False,
        scope='FULL_VIDEO_ROUTED_CANDIDATE_ONLY', checks={}, errors={},
        cdc_drc_exception_review='REQUIRED_ON_THIS_DCP; historical policy is comparison only',
        full_current_graph_numeric='NOT_RUN', board='NOT_RUN', end_to_end_p95='NOT_MEASURED')
    def check(name, fn):
        try:
            result['checks'][name] = fn()
        except Exception as exc:
            result['errors'][name] = str(exc)
    def require(value, message):
        if not value:
            raise ValueError(message)
        return True
    report = lambda name: (FOLDER/(name+'.rpt')).read_text(errors='replace')
    status = json.loads((FOLDER/'status.json').read_text())
    check('build_complete', lambda: require(status.get('returncode') == 0 and
        status.get('source_unchanged') is True and status.get('candidate_complete') is True,
        'Incomplete build or changed source'))
    manifest = json.loads((FOLDER/'source_manifest.json').read_text())
    check('source_binding', lambda: require(all(sha(ROOT/name) == digest for name, digest in manifest.items()),
        'A frozen implementation input changed'))
    references = json.loads((ROOT/'source_origin.json').read_text())['reference_files']
    check('reference_binding', lambda: require(len(references) == 3 and all(
        sha(ROOT/name) == info['sha256'] for name, info in references.items()), 'Historical comparison reference changed'))
    result['reference_sha256'] = {name:info['sha256'] for name,info in references.items()}
    check('xdc_identity', lambda: require(sha(ROOT/'hardware/constraints/ax7020_ddr_vdma_ov5640_hdmi.xdc') ==
        policy['preserved_xdc_sha256'], 'Physical constraints changed'))
    def sensor_static():
        data = audit(ROOT, 24_000_000)
        require(data['status'] == 'PASS_STATIC_ONLY', 'Sensor static clock/AE check failed')
        return data
    check('sensor_static', sensor_static)
    facts = dict(line.split('\t', 1) for line in (FOLDER/'facts.tsv').read_text().splitlines())
    check('direct_facts', lambda: require(all(facts.get(name) == '0' for name in
        ('setup_bad', 'hold_bad', 'drc_blocking', 'locked_ips')), 'Blocking Vivado fact: '+str(facts)))
    clocks = {name[6:]: float(value) for name, value in facts.items() if name.startswith('clock:')}
    check('clock_inventory', lambda: require(set(clocks) == set(policy['clock_periods_ns']) and
        all(abs(clocks[name]-period) < .001 for name, period in policy['clock_periods_ns'].items()),
        'Clock inventory or period changed: '+str(clocks)))
    result['clocks_ns'] = clocks
    prefix = (FOLDER/'synthesis_accelerator.tsv').read_text().splitlines()[0].split('\t')[-1]
    check('module_identity', lambda: require(re.fullmatch(r'system_conv_accel_0_\d+', prefix) is not None,
        'Unexpected accelerator module'))
    check('timing', lambda: base.check_timing_summary(report('timing_summary')))
    check('resources', lambda: base.check_resources(report('utilization'), prefix))
    check('route', lambda: base.check_route(facts))
    check('route_counts', lambda: base.check_route_report(report('route_status')))
    check('io_constraints', lambda: base.check_constraints(report('check_timing'), policy))
    check('accelerator_hierarchy', lambda: base.check_accelerator_evidence(FOLDER, prefix))
    check('generated_ip', lambda: base.check_generated_ip(prefix))
    check('ignored_exceptions', lambda: require(report('exceptions_ignored').split('Exceptions Report')[-1].strip() ==
        'No ignored timing exceptions found.', 'Ignored timing exceptions found'))
    def camera():
        rows = [line.split('\t') for line in (FOLDER/'camera_input_facts.tsv').read_text().splitlines()]
        rows = [(port, kind, float(value)) for port, kind, value in rows if not port.startswith('clock:')]
        expected = {(port, kind) for port in ['cam_data[%d]' % bit for bit in range(8)]+['cam_href', 'cam_vsync']
                    for kind in ('max', 'min')}
        require(len(rows) == 20 and {(p,k) for p,k,v in rows} == expected and min(v for p,k,v in rows) >= 0,
            'Actual DVP input timing coverage or slack failed')
        return {p+'|'+k:v for p,k,v in rows}
    check('camera_input_setup_hold', camera)
    # These calls check exact inventories against the old policy as a comparison.
    # Matching warnings do not rebind the old netlist proof to this candidate.
    result['reference_comparison'] = {}
    for name, fn in [('cdc', lambda: v33.check_cdc_report(report('cdc'), policy)),
                     ('cdc_unwaived', lambda: v33.check_cdc_report(report('cdc_unwaived_view'), policy)),
                     ('drc', lambda: base.check_drc(report('drc'), policy)),
                     ('bus_skew', lambda: base.check_bus_skew(report('bus_skew'), policy))]:
        try:
            result['reference_comparison'][name] = dict(status='MATCHES_REFERENCE_REQUIRES_FRESH_PROOF', detail=fn())
        except Exception as exc:
            result['reference_comparison'][name] = dict(status='REVIEW_REQUIRED', reason=str(exc))
    result['artifacts'] = {name:dict(path=str(FOLDER/name), sha256=sha(FOLDER/name), bytes=(FOLDER/name).stat().st_size)
        for name in ['camera_v32_384.bit', 'camera_v32_384.hwh', 'final_routed.dcp']}
    result['evidence_sha256'] = {p.relative_to(FOLDER).as_posix():sha(p) for p in FOLDER.rglob('*')
        if p.is_file() and p.suffix in ('.rpt', '.tsv', '.txt')}
    if not result['errors']:
        result['status'] = 'ROUTED_TIMING_RESOURCE_PASS_EXCEPTION_PROOF_REVIEW_REQUIRED'
    (FOLDER/'FINAL_ROUTED_GATE.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({k:result[k] for k in ('status', 'deployment_allowed', 'errors')}, indent=2))
    return 1 if result['errors'] else 0

if __name__ == '__main__':
    sys.exit(main())
