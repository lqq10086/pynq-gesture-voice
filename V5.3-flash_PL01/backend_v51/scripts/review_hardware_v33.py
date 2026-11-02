"""Fail-closed V3.3 routed gate using explicitly reviewed, source-bound exceptions."""
import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys

import review_hardware_384_D as base
from check_ov5640_clock import audit

ROOT = Path(__file__).resolve().parents[1]
require = base.require


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bound_files(entries):
    for name, expected in entries.items():
        require(sha(ROOT / name.replace('\\', '/')) == expected, 'Changed bound input: ' + name)


def check_cdc_report(text, policy):
    """Reconcile every detailed CDC row with the report's per-rule inventory."""
    header = text.split('Source Clock:', 1)[0]
    summaries = re.findall(r'^(CDC-\d+)\s+(Info|Warning|Critical)\s+(\d+)\s+.+$',
                           header, re.M)
    summary_lines = re.findall(r'^CDC-\S+\s+.*$', header, re.M)
    require(summaries and len(summaries) == len(summary_lines),
            'Missing or unparseable CDC summary inventory')
    expected = {(rule, severity): int(count) for rule, severity, count in summaries}
    require(len(expected) == len(summaries), 'Duplicate CDC summary rule')
    observed = {}
    rows = base.cdc_rows(text)
    for row in rows:
        key = (row['id'], row['severity'])
        observed[key] = observed.get(key, 0) + 1
    require(observed == expected,
            'CDC detail/summary count mismatch: observed=%r expected=%r' % (observed, expected))
    require(len({base.cdc_key(row) for row in rows}) == len(rows), 'Duplicate CDC detailed path')
    result = base.check_cdc(text, policy)
    result['summary_inventory'] = {'|'.join(key): count for key, count in expected.items()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report-name', required=True)
    parser.add_argument('--policy', type=Path, default=ROOT/'scripts/hardware_reviewed_video_exceptions_v33.json')
    args = parser.parse_args()
    folder = ROOT/'reports/hardware'/args.report_name
    status = json.loads((folder/'status.json').read_text())
    require(status.get('returncode') == 0 and status.get('source_unchanged') is True and
            status.get('candidate_complete') is True, 'Incomplete or changed build')
    policy = json.loads(args.policy.read_text())
    require(policy.get('version') == 'V3.3_720P30_VIDEO_PIPELINE', 'Wrong review policy')
    require(bool(policy.get('evidence_sha256')), 'Review policy lacks bound netlist evidence')
    bound_files(policy['evidence_sha256'])
    source = json.loads((folder/'source_manifest.json').read_text())
    bound_files(source)
    require(sha(ROOT/'hardware/constraints/ax7020_ddr_vdma_ov5640_hdmi.xdc') ==
            policy['preserved_xdc_sha256'], 'Unreviewed physical constraints')
    static = audit(ROOT, 24_000_000)
    require(static['status'] == 'PASS_STATIC_ONLY', 'Sensor static clock/AE gate failed')
    rtl = json.loads((ROOT/'reports/hardware/rtl_sim/verification.json').read_text())
    require(rtl['status'] == 'PASS' and len(rtl['tests']) == 4, 'Missing video RTL simulation')
    bound_files(rtl['input_sha256'])
    for test in rtl['tests']:
        require(test['status'] == 'PASS' and sha(ROOT/test['log'].replace('\\', '/')) == test['sha256'],
                'Video RTL log identity mismatch')
    hls_inputs = None
    for stage in ('csim', 'synthesis', 'cosim'):
        record = json.loads((ROOT/'reports/hardware/hls_D'/f'{stage}_status.json').read_text())
        require(record['status'] == 'PASS' and record['source_unchanged'] is True and record['input_sha256'],
                'Missing source-bound HLS stage')
        bound_files(record['input_sha256'])
        require(hls_inputs is None or record['input_sha256'] == hls_inputs, 'Different HLS input versions')
        hls_inputs = record['input_sha256']
    cosim = (ROOT/'reports/hardware/hls_D/cosim.log').read_text(errors='replace')
    require('*** C/RTL co-simulation finished: PASS ***' in cosim and
            'V32_ACTUAL_INTEGER_REFERENCE_PASS count9' in cosim, 'Missing actual integer RTL PASS')
    cycles = json.loads((ROOT/'reports/hardware/hls_D/cycles.json').read_text())
    require(bool(cycles.get('transactions_sha256')), 'Missing RTL transaction identity')
    require(cycles['source_sha256'] == sha(ROOT/'hls/spatial4/conv_accel.cpp'), 'Wrong RTL cycle source')
    for name, expected in cycles['fixture_sha256'].items():
        require(sha(ROOT/'reports/hardware/hls/fixtures'/name) == expected, 'Changed RTL cycle fixture')
    cycle_run = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/check_spatial4_cycles.py'),
        '--candidate', str(ROOT/'reports/hardware/hls_D/cycles.json')], check=True, capture_output=True, text=True)
    report = lambda name: (folder/(name+'.rpt')).read_text(errors='replace')
    facts = dict(line.split('\t', 1) for line in (folder/'facts.tsv').read_text().splitlines())
    require(all(facts.get(name) == '0' for name in ('setup_bad', 'hold_bad', 'drc_blocking', 'locked_ips')),
            'Blocking direct Vivado fact')
    clocks = {name[6:]: float(value) for name, value in facts.items() if name.startswith('clock:')}
    require(set(clocks) == set(policy['clock_periods_ns']), 'Unreviewed clock inventory')
    require(all(abs(clocks[name]-period) < .001 for name, period in policy['clock_periods_ns'].items()),
            'Unexpected clock period')
    wrapper_line = (folder/'synthesis_accelerator.tsv').read_text().splitlines()[0].split('\t')
    module_prefix = wrapper_line[-1]
    require(re.fullmatch(r'system_conv_accel_0_\d+', module_prefix), 'Unexpected accelerator wrapper module')
    camera_paths = {}
    for line in (folder/'camera_input_facts.tsv').read_text().splitlines():
        port, kind, value = line.split('\t')
        if not port.startswith('clock:'):
            require((port, kind) not in camera_paths, 'Duplicate camera timing path')
            camera_paths[(port, kind)] = float(value)
    expected_ports = ['cam_data[%d]' % bit for bit in range(8)] + ['cam_href', 'cam_vsync']
    require(set(camera_paths) == {(port, kind) for port in expected_ports for kind in ('max', 'min')},
            'Incomplete actual DVP input setup/hold coverage')
    require(min(camera_paths.values()) >= 0, 'Actual DVP input timing failure')
    ignored = report('exceptions_ignored').split('Exceptions Report')[-1].strip()
    require(ignored == 'No ignored timing exceptions found.', 'Ignored timing exception requires diagnosis')
    result = dict(status='V33_ROUTED_TIMING_CDC_PASS', report_directory=str(folder),
        policy_sha256=sha(args.policy), source_manifest_sha256=sha(folder/'source_manifest.json'),
        clocks_ns=clocks, sensor_static=static,
        timing=base.check_timing_summary(report('timing_summary')),
        resources=base.check_resources(report('utilization'), module_prefix),
        route=base.check_route(facts), route_counts=base.check_route_report(report('route_status')),
        io_exclusions=base.check_constraints(report('check_timing'), policy),
        cdc=check_cdc_report(report('cdc'), policy),
        cdc_unwaived=check_cdc_report(report('cdc_unwaived_view'), policy),
        drc=base.check_drc(report('drc'), policy),
        bus_skew_slacks_ns=base.check_bus_skew(report('bus_skew'), policy),
        camera_input_slack_ns={port:{kind:camera_paths[(port,kind)] for kind in ('max','min')} for port in expected_ports},
        accelerator=base.check_accelerator_evidence(folder, module_prefix), generated_ip=base.check_generated_ip(module_prefix),
        hls_cycles=json.loads(cycle_run.stdout), video_rtl_verification_sha256=sha(ROOT/'reports/hardware/rtl_sim/verification.json'),
        hardware_sha256={suffix:sha(folder/('camera_v32_384.'+suffix)) for suffix in ('bit','hwh')},
        evidence_sha256={path.name:sha(path) for path in folder.iterdir() if path.suffix in ('.rpt','.tsv')},
        board='NOT_IMPLIED; board snapshots, FPS and fresh numeric qualification are separate')
    (ROOT/'reports/VIDEO_HARDWARE_GATE.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({key:result[key] for key in ('status','timing','resources','hardware_sha256')}, indent=2))


if __name__ == '__main__':
    main()
