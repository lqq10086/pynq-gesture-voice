"""Fail closed on new timing, resource, CDC or DRC failures; no board writes."""
import argparse,hashlib,json,re,importlib.util,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def require(condition, message):
    if not condition:
        raise ValueError(message)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def check_resources(text, module_prefix='system_conv_accel_0_0'):
    expected = ['Instance', 'Module', 'Total LUTs', 'Logic LUTs', 'LUTRAMs', 'SRLs',
                'FFs', 'RAMB36', 'RAMB18', 'DSP Blocks']
    rows = [[field.strip() for field in line.strip().strip('|').split('|')]
            for line in text.splitlines() if line.lstrip().startswith('|')]
    require(expected in rows, 'Missing expected hierarchical utilization columns')
    top = [row for row in rows if len(row) == len(expected) and row[0] == 'system_top']
    require(len(top) == 1 and top[0][1] == '(top)', 'Missing unique full-design utilization row')
    def resource_values(row):
        values = dict(zip(expected[2:], map(int, row[2:])))
        require(all(value >= 0 for value in values.values()), 'Negative utilization count')
        return {'LUT': values['Total LUTs'], 'FF': values['FFs'],
                'BRAM18_equivalent': 2 * values['RAMB36'] + values['RAMB18'],
                'DSP': values['DSP Blocks'], 'LUT_memory': values['LUTRAMs'] + values['SRLs']}
    used = resource_values(top[0])
    wrapper = [row for row in rows if len(row) == len(expected) and row[0] == 'conv_accel_0']
    core = [row for row in rows if len(row) == len(expected)
            and row[0] == 'inst' and row[1] == module_prefix + '_conv_accel']
    all_cores = [row for row in rows if len(row) == len(expected)
                 and not row[0].startswith('(') and row[1].endswith('_conv_accel')]
    require(len(wrapper) == len(core) == len(all_cores) == 1
            and wrapper[0][1] == module_prefix,
            'Missing or duplicate replacement accelerator hierarchy in utilization')
    exclusive = resource_values(wrapper[0])
    require(exclusive == resource_values(core[0]), 'Accelerator wrapper/core resource mismatch')
    require(all(0 < exclusive[key] <= used[key] for key in ('LUT', 'FF', 'BRAM18_equivalent', 'DSP')),
            'Invalid exclusive accelerator utilization')
    remaining = {key: value - exclusive[key] for key, value in used.items()}
    require(all(value >= 0 for value in remaining.values()), 'Accelerator exceeds full-design utilization')
    caps = {'LUT': 53200, 'FF': 106400, 'BRAM18_equivalent': 280, 'DSP': 220, 'LUT_memory': 17400}
    require(all(0 <= value <= caps[key] for key, value in used.items()),
            'XC7Z020 full-design resource capacity exceeded: ' + str(used))
    require(all(used[key] > 0 for key in ('LUT', 'FF', 'BRAM18_equivalent', 'DSP')),
            'Empty full-design resource coverage')
    return {'used': used, 'capacity': caps, 'full_design_total': used,
            'exclusive_accelerator_core': exclusive,
            'remaining_video_control_shared': remaining,
            'core_hierarchy': 'system_i/system_i/conv_accel_0/inst',
            'accounting': 'Exact accelerator wrapper/core hierarchy rows; remainder is total minus exclusive core and includes video, PS/control/interconnect/shared resources. No allocation of shared resources to AI.'}

def check_accelerator_evidence(report_dir, module_prefix='system_conv_accel_0_0'):
    bd = (report_dir / 'accelerator_bd.tsv').read_text().splitlines()
    require(bd == ['conv_accel_0\txilinx.com:hls:conv_accel:3.2'], 'Expected exactly one replacement accelerator in BD')
    expected = ['wrapper\tsystem_i/system_i/conv_accel_0\t' + module_prefix,
                'core\tsystem_i/system_i/conv_accel_0/inst\t' + module_prefix + '_conv_accel']
    hashes = {'accelerator_bd.tsv': sha(report_dir / 'accelerator_bd.tsv')}
    for stage in ('synthesis', 'routed'):
        path = report_dir / (stage + '_accelerator.tsv')
        require(path.read_text().splitlines() == expected,
                'Expected exactly one replacement accelerator in ' + stage + ' netlist')
        hashes[path.name] = sha(path)
    return {'bd_instances': 1, 'synthesis_cores': 1, 'routed_cores': 1, 'evidence_sha256': hashes}

def check_generated_ip(module_prefix='system_conv_accel_0_0'):
    project = ROOT / 'hardware_build/vivado_D'
    exported = ROOT / 'hardware_build/hls384_D/solution1/impl/ip/hdl/verilog'
    tops = list((project / 'camera_v32_384.gen/sources_1/bd/system/ipshared').rglob('conv_accel.v'))
    require(len(tops) == 1, 'Expected exactly one generated HLS top source')
    inventory = lambda folder: {path.name: sha(path) for path in folder.iterdir() if path.is_file()}
    expected = inventory(exported)
    generated = inventory(tops[0].parent)
    # Vivado keeps exported .v in ipshared but relocates initialization data
    # under the configured IP instance. Both sets must match the frozen export.
    instance_data = project / ('camera_v32_384.gen/sources_1/bd/system/ip/' + module_prefix + '/hdl/verilog')
    for path in instance_data.glob('*.dat'):
        require(path.name not in generated, 'Duplicate generated HLS data file')
        generated[path.name] = sha(path)
    require(expected and generated == expected,
            'Generated Vivado HLS RTL/data differs from frozen new export')
    return {'status': 'PASS', 'files': expected, 'generated_folder': str(tops[0].parent)}

def check_timing_summary(text):
    section = text.split('| Design Timing Summary')[-1].split('| Clock Summary')[0]
    rows = re.findall(r'^\s*((?:-?\d+(?:\.\d+)?\s+){11}-?\d+(?:\.\d+)?)\s*$', section, re.M)
    require(len(rows) == 1, 'Missing or ambiguous Design Timing Summary')
    values = list(map(float, rows[0].split()))
    names = 'WNS TNS setup_failed setup_total WHS THS hold_failed hold_total WPWS TPWS pulse_failed pulse_total'.split()
    data = dict(zip(names, values))
    for name in ('WNS', 'WHS', 'WPWS'):
        require(data[name] >= 0, f'Negative {name}: {data[name]}')
    for name in ('TNS', 'THS', 'TPWS', 'setup_failed', 'hold_failed', 'pulse_failed'):
        require(data[name] == 0, f'Failing timing {name}: {data[name]}')
    require(all(data[name] > 0 for name in ('setup_total', 'hold_total', 'pulse_total')), 'Empty timing coverage')
    return data

def check_constraints(text, policy):
    rows = re.findall(r'checking (\w+) \((\d+)\)', text)
    required = {'no_clock', 'constant_clock', 'pulse_width_clock', 'unconstrained_internal_endpoints',
                'no_input_delay', 'no_output_delay', 'multiple_clock', 'generated_clocks',
                'loops', 'partial_input_delay', 'partial_output_delay', 'latch_loops'}
    require(set(name for name, count in rows) == required, 'Incomplete check_timing categories')
    for name, count in rows:
        if name not in ('no_input_delay', 'no_output_delay'):
            require(int(count) == 0, f'check_timing {name}={count}')
    accepted = {}
    for direction in ('input', 'output'):
        name = f'no_{direction}_delay'
        # Last occurrence is the detailed section, rather than table of contents.
        section = text[text.rfind(f'checking {name} ('):]
        section = re.split(r'\n\d+\. checking ', section, maxsplit=1)[0]
        bare = re.search(r'There are (\d+) (?:input )?ports with no ' + direction + r' delay specified', section)
        require(bare is not None and int(bare[1]) == 0, f'Unreviewed missing {direction} delay')
        match = re.search(r'There are (\d+) (?:input )?ports with no ' + direction +
                          r' delay but user has a false path constraint[^\n]*\n\s*\n([^\n]+(?:\n[^\n]+)*)', section)
        count = int(dict(rows)[name])
        if count:
            require(match is not None, f'Missing {direction} exclusion detail')
            ports = match[2].split()
            require(len(ports) == count == int(match[1]), f'{direction} exclusion count mismatch')
        else:
            ports = []
        require(set(ports) <= set(policy['io'][direction]), f'Unexpected {direction} exclusions: {ports}')
        # Other omissions (e.g. propagated-clock output exceptions) are not silently accepted.
        counts = [int(value) for value in re.findall(r'There are (\d+)', section)]
        require(sum(counts) == count, f'Additional {direction} omissions')
        accepted[direction] = {port: policy['io'][direction][port] for port in ports}
    return accepted

def cdc_rows(text):
    rows = []
    src = dst = None
    for line in text.splitlines():
        if line.startswith('Source Clock: '): src = line.split(': ', 1)[1]
        if line.startswith('Destination Clock: '): dst = line.split(': ', 1)[1]
        match = re.match(r'\s*\d+\s+(CDC-\d+)\s+(Info|Warning|Critical)\s+', line)
        if match:
            endpoints = re.search(r'\s(\S+)\s+(\S+)\s*$', line)
            require(src and dst and endpoints, f'Unparseable CDC row: {line}')
            rows.append({'id': match[1], 'severity': match[2], 'src_clock': src, 'dst_clock': dst,
                         'source': endpoints[1], 'destination': endpoints[2]})
    require(rows, 'Empty CDC report')
    return rows

def cdc_key(row):
    return '|'.join(row[key] for key in ('id', 'src_clock', 'dst_clock', 'source', 'destination'))

def check_cdc(text, policy, raw=False):
    rows = cdc_rows(text)
    allowed = policy['cdc_paths']
    accepted = []
    for row in rows:
        if row['severity'] == 'Critical':
            reason = policy.get('critical_cdc_paths', {}).get(cdc_key(row))
            require(reason is not None, f'Unreviewed critical CDC: {row}')
            accepted.append(dict(row, reason=reason))
        if row['severity'] == 'Warning':
            key = cdc_key(row)
            reason = allowed.get(key)
            if raw and not reason:
                # Only vendor's documented bundled-data handshake waiver, on named instances.
                for instance in ('control_bridge_i/command_cdc_i', 'control_bridge_i/result_cdc_i', 'color_commit_i/frame_cdc_i'):
                    if (row['id'] == 'CDC-15' and
                        re.fullmatch(re.escape(instance) + r'/src_hsdata_ff_reg\[\d+\]/C', row['source']) and
                        re.fullmatch(re.escape(instance) + r'/dest_hsdata_ff_reg\[\d+\]/D', row['destination'])):
                        reason = 'Installed XPM handshake scoped CDC-15 waiver; held source data, synchronized request/ack; max-delay and bus-skew gate required.'
            require(reason is not None, f'Unreviewed CDC path: {row}')
            accepted.append(dict(row, reason=reason))
    return {'counts': {severity: sum(r['severity'] == severity for r in rows) for severity in ('Info', 'Warning', 'Critical')},
            'accepted': accepted}

def check_bus_skew(text, policy=None):
    values = [float(value) for value in re.findall(r'Slack\s*\([^)]*\)\s*:\s*(-?\d+(?:\.\d+)?)\s*ns', text)]
    require(values, 'No parsed bus-skew paths; expected XPM bundle constraints')
    require(all(value >= 0 for value in values) and 'VIOLATED' not in text, 'Negative bus skew slack')
    blocks = re.split(r'^Id:\s*\d+\s*$', text, flags=re.M)[1:]
    for block in blocks:
        if re.search(r'Slack\s*\([^)]*\)\s*:', block):
            continue
        exact = ' '.join(block.split())
        require(exact in (policy or {}).get('single_bit_skew_bodies', {}),
                'Missing detailed bus-skew coverage for a constraint')
    return values

def check_route(facts):
    require(facts.get('ROUTED_FULLY') == '1', 'Design not fully routed')
    require(facts.get('ERRORS_IN_ROUTES') == '0', 'Routing errors or missing route facts')
    require(facts.get('PARTIALLY_ROUTED') == '0', 'Partially routed design')
    return True

def check_route_report(text):
    require(text.startswith('Design Route Status'), 'Unparseable route status report')
    counts = {}
    for key, label in (('routable', 'routable nets'), ('fully_routed', 'fully routed nets'),
                       ('errors', 'nets with routing errors')):
        matches = re.findall(r'# of ' + label + r'\.+\s*:\s*(\d+)\s*:', text)
        require(len(matches) == 1, 'Missing or ambiguous route metric ' + key)
        counts[key] = int(matches[0])
    require(counts['routable'] == counts['fully_routed'] > 0 and counts['errors'] == 0,
            'Unrouted or erroneous nets in route report')
    return counts

def drc_blocks(text):
    return [(m[1], m[2], ' '.join(m[3].split())) for m in re.finditer(
        r'^([\w-]+)#\d+ (Warning|Advisory|Error|Critical Warning)\n(.*?)(?=^[\w-]+#\d+ |\Z)', text, re.M | re.S)]

def check_drc(text, policy):
    blocks = drc_blocks(text)
    count = re.search(r'Violations found:\s*(\d+)', text)
    require(count and len(blocks) == int(count[1]), 'DRC detailed count mismatch')
    accepted = []
    for rule, severity, body in blocks:
        require(severity not in ('Error', 'Critical Warning'), f'Blocking DRC: {rule}')
        reason = policy['drc_details'].get(rule + '|' + body)
        if not reason and rule in ('DPIP-1', 'DPOP-1', 'DPOP-2') and body.startswith(('Input pipelining DSP ', 'PREG Output pipelining DSP ', 'MREG Output pipelining DSP ')):
            reason = 'DSP pipeline performance recommendation only; implemented setup/hold/pulse-width and resource gates must pass.'
        require(reason is not None, f'Unreviewed DRC {rule}: {body}')
        accepted.append({'rule': rule, 'severity': severity, 'detail': body, 'reason': reason})
    return accepted

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--report-name',default='D_build');args=ap.parse_args();folder=ROOT/'reports/hardware'/args.report_name
    status=json.loads((folder/'status.json').read_text())
    require(status.get('returncode')==0 and status.get('source_unchanged') is True and status.get('candidate_complete') is True,'Build must finish successfully with unchanged inputs')
    require('V32_FULL_DESIGN_CANDIDATE_COMPLETE_REQUIRES_ROUTED_REVIEW' in (folder/'build_result.txt').read_text(),'Full candidate completion marker missing')
    policy=json.loads((ROOT/'scripts/hardware_reviewed_video_exceptions.json').read_text())
    require(sha(ROOT/'hardware/constraints/ax7020_ddr_vdma_ov5640_hdmi.xdc')==policy['preserved_xdc_sha256'],'Physical constraints drifted')
    source=json.loads((folder/'source_manifest.json').read_text());source={Path(p).as_posix():h for p,h in source.items()};require(all(sha(ROOT/p)==h for p,h in source.items()),'Build input changed during/after implementation')
    cosim=json.loads((ROOT/'reports/hardware/hls_D/cosim_status.json').read_text())
    require(cosim.get('status')=='PASS' and cosim.get('source_sha256')==source['hls/spatial4/conv_accel.cpp'],'Formal RTL evidence does not match implemented core source')
    stages=[json.loads((ROOT/'reports/hardware/hls_D'/(name+'_status.json')).read_text()) for name in ['csim','synthesis','cosim']]
    require(all(s.get('status')=='PASS' and s.get('source_unchanged') is True and s.get('input_sha256') for s in stages),'Incomplete source-bound HLS stages')
    require(all(s['input_sha256']==stages[0]['input_sha256'] for s in stages),'HLS stages used different inputs')
    require(all(source.get(p)==h for p,h in stages[0]['input_sha256'].items()),'Implemented source/fixtures differ from RTL-verified inputs')
    provenance=json.loads((ROOT/'reports/hardware/source_provenance.json').read_text())
    for name in ['conv_accel.h','silu_q8_lut.h','silu_q16_lut.h']:
        require(source['hls/spatial4/'+name]==provenance['source_sha256'][name],'Unverified header/LUT change: '+name)
    cycles=json.loads((ROOT/'reports/hardware/hls_D/cycles.json').read_text())
    require(cycles['source_sha256']==source['hls/spatial4/conv_accel.cpp'],'RTL cycle record source mismatch')
    require(bool(cycles.get('transactions_sha256')),'Missing RTL transaction evidence binding')
    performance=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/check_spatial4_cycles.py'),'--candidate',str(ROOT/'reports/hardware/hls_D/cycles.json')],cwd=ROOT,check=True,capture_output=True,text=True)
    performance=json.loads(performance.stdout)
    require(all(sha(ROOT/'reports/hardware/hls/fixtures'/name)==h for name,h in cycles['fixture_sha256'].items()),'Recorded384 RTL fixtures changed')
    log=(ROOT/'reports/hardware/hls_D/cosim.log').read_text(errors='replace')
    require('V32_ACTUAL_INTEGER_REFERENCE_PASS count9' in log and '*** C/RTL co-simulation finished: PASS ***' in log,'Actual384 RTL verification is incomplete')
    report=lambda name:(folder/(name+'.rpt')).read_text(errors='replace')
    facts=dict(line.split('\t',1) for line in (folder/'facts.tsv').read_text().splitlines())
    require(facts.get('setup_bad')==facts.get('hold_bad')==facts.get('drc_blocking')==facts.get('locked_ips')=='0','Blocking direct Vivado metric')
    require(facts.get('accel_vlnv')=='xilinx.com:hls:conv_accel:3.2','Wrong core')
    for name,period in policy['clock_periods_ns'].items():require(abs(float(facts.get('clock:'+name,'nan'))-period)<.001,'Unexpected clock '+name)
    require(set(k[6:] for k in facts if k.startswith('clock:'))==set(policy['clock_periods_ns']),'Clock inventory mismatch')
    if (folder/'postroute_review.json').exists():
        spec=importlib.util.spec_from_file_location('postroute_review',ROOT/'scripts/review_postroute_384_D.py');review=importlib.util.module_from_spec(spec);spec.loader.exec_module(review)
        reviewed=review.read_review(folder,drc_blocks(report('drc')),report('bus_skew'))
        for category in ['drc_details','single_bit_skew_bodies']:policy[category].update(reviewed[category])
    result={'status':'PASS','rtl_hotspot_cycle_gate':performance,'timing':check_timing_summary(report('timing_summary')),'resources':check_resources(report('utilization')),'route':check_route(facts),'route_counts':check_route_report(report('route_status')),'io_exclusions':check_constraints(report('check_timing'),policy),'cdc':check_cdc(report('cdc'),policy),'cdc_unwaived':check_cdc(report('cdc_unwaived_view'),policy,raw=True),'drc':check_drc(report('drc'),policy),'bus_skew_slacks_ns':check_bus_skew(report('bus_skew'),policy),'accelerator':check_accelerator_evidence(folder),'generated_ip':check_generated_ip(),'facts':facts,'board':'PENDING'}
    ignored=report('exceptions_ignored').split('Exceptions Report')[-1].strip();require(ignored=='No ignored timing exceptions found.','Ignored timing exceptions require review')
    result['artifacts']={name:{'path':str(folder/name),'sha256':sha(folder/name),'bytes':(folder/name).stat().st_size} for name in ['camera_v32_384.bit','camera_v32_384.hwh']}
    (folder/'hardware_gate.json').write_text(json.dumps(result,indent=2));print(json.dumps({'status':result['status'],'timing':result['timing'],'resources':result['resources'],'artifacts':result['artifacts']},indent=2))
if __name__=='__main__':main()
