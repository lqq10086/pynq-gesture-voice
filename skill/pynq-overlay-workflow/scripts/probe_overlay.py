"""Metadata-only by default. --load downloads; --read opts into declared safe reads."""
import argparse
import json
from overlay_contract import load_contract


def inspect(cfg, files, overlay_factory, mmio_factory, load=False, read=False):
    overlay = overlay_factory(str(files['bit']), download=load)
    results = []
    # Validate ALL IPs before creating ANY MMIO object.
    for ip in cfg['ips']:
        info = overlay.ip_dict.get(ip['name'])
        if info is None:
            raise ValueError('IP absent in HWH: ' + ip['name'])
        for key in ('type', 'phys_addr', 'addr_range'):
            if info.get(key) != ip[key]:
                raise ValueError('HWH mismatch: %s.%s' % (ip['name'], key))
        results.append({'name': ip['name'], 'metadata': 'PASS', 'reads': []})
    loaded = bool(overlay.is_loaded())
    report = {'status': 'PASS', 'scope': 'metadata_only', 'target': cfg['target_label'],
              'build_id': cfg['build_id'], 'download_requested': load, 'is_loaded': loaded,
              'identity_read': 'NOT_RUN', 'ips': results}
    if load and not loaded:
        raise RuntimeError('download requested but expected overlay not loaded')
    if not read:
        return report
    report['scope'] = 'selected_safe_reads'
    if not loaded:
        report.update(status='NOT_RUN', reason='expected overlay is not loaded; no MMIO performed')
        return report
    if not any(ip['safe_reads'] for ip in cfg['ips']):
        report.update(status='NOT_RUN', reason='no safe registers declared')
        return report
    for ip, row in zip(cfg['ips'], results):
        if not ip['safe_reads']:
            continue
        mmio = mmio_factory(ip['phys_addr'], ip['addr_range'])
        for spec in ip['safe_reads']:
            value = int(mmio.read(spec['offset']))
            passed = (value & spec['mask']) == spec['expected']
            row['reads'].append({'name': spec['name'], 'offset': spec['offset'],
                                 'value': value, 'mask': spec['mask'],
                                 'expected': spec['expected'], 'status': 'PASS' if passed else 'FAIL'})
            if not passed:
                report['status'] = 'FAIL'
                report['identity_read'] = 'FAIL'
                report['reason'] = 'identity/value mismatch; further hardware reads stopped'
                return report
    report['identity_read'] = report['status']
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract')
    parser.add_argument('--load', action='store_true')
    parser.add_argument('--read', action='store_true')
    args = parser.parse_args()
    try:
        cfg, files, hashes = load_contract(args.contract)
        try:
            import pynq
        except ImportError as exc:
            print(json.dumps({'status': 'NOT_RUN', 'reason': 'PYNQ unavailable in this interpreter',
                              'detail': str(exc), 'scope': 'board_probe'}))
            return 2
        report = inspect(cfg, files, pynq.Overlay, pynq.MMIO, args.load, args.read)
        report['pynq_version'] = getattr(pynq, '__version__', 'unknown')
        report['hashes'] = hashes
        print(json.dumps(report, ensure_ascii=False))
        return {'PASS': 0, 'FAIL': 1, 'NOT_RUN': 2}[report['status']]
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'scope': 'board_probe', 'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
