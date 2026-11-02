"""Run the V3.3 passive-observer/CDC and DVP parser XSIM regressions."""
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VIVADO = Path('D:/Tools/FPGA/Vivado/2023.1')
OUT = ROOT / 'reports/hardware/rtl_sim'
SOURCES = [
    ROOT / 'hardware/rtl' / name for name in (
        'stats_snapshot_cdc.v', 'video_axis_monitor.v', 's2mm_write_observer.v',
        'video_stats_snapshot.v', 'camera_dvp_to_axis_rgb565.v')
] + [ROOT / 'tests/video_stats_tb.sv', ROOT / 'tests/camera_dvp_720p30_tb.sv']
TESTS = [
    ('video_stats_tb', 'snapshot_sim', 'V33_SNAPSHOT_CDC_PASS'),
    ('s2mm_write_observer_tb', 's2mm_observer_sim', 'V33_S2MM_OBSERVER_PASS'),
    ('video_axis_monitor_tb', 'axis_monitor_sim', 'V33_AXIS_MONITOR_PASS'),
    ('camera_dvp_720p30_tb', 'dvp_parser_sim', 'V33_DVP_PASS'),
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(tool, args, log):
    with log.open('w', encoding='utf-8') as output:
        subprocess.run([str(VIVADO / 'bin' / (tool + '.bat')), *map(str, args)],
                       cwd=OUT, stdout=output, stderr=subprocess.STDOUT, check=True)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    inputs = {str(p.relative_to(ROOT)): sha(p) for p in SOURCES}
    run('xvlog', ['-sv', VIVADO / 'data/ip/xpm/xpm_cdc/hdl/xpm_cdc.sv',
                 VIVADO / 'data/verilog/src/glbl.v', *SOURCES], OUT / 'compile_console.log')
    results = []
    for top, name, marker in TESTS:
        run('xelab', [top, 'glbl', '-s', name], OUT / (name + '_elab.log'))
        log = OUT / (name + '.log')
        run('xsim', [name, '-runall', '-log', log], OUT / (name + '_console.log'))
        text = log.read_text(errors='replace')
        if marker not in text or 'Fatal:' in text or 'ERROR:' in text:
            raise SystemExit(f'Failed simulation: {log}')
        results.append({'top': top, 'status': 'PASS', 'log': str(log.relative_to(ROOT)), 'sha256': sha(log)})
    if inputs != {str(p.relative_to(ROOT)): sha(p) for p in SOURCES}:
        raise SystemExit('RTL sources changed during simulation')
    (OUT / 'verification.json').write_text(json.dumps({
        'status': 'PASS', 'tool': 'Vivado XSIM 2023.1', 'input_sha256': inputs,
        'tests': results, 'board': 'NOT_MEASURED'}, indent=2) + '\n')
    print('V33_RTL_REGRESSIONS_PASS', len(results))


if __name__ == '__main__':
    main()
