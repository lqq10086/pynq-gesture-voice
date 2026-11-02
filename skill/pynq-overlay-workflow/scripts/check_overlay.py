"""Offline file/hash/XML/contract check. Never imports PYNQ or touches hardware."""
import argparse
import json
from overlay_contract import load_contract


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract')
    args = parser.parse_args()
    try:
        cfg, files, hashes = load_contract(args.contract)
        print(json.dumps({'status': 'PASS', 'scope': 'offline_contract_and_files_only',
                          'target': cfg['target_label'], 'build_id': cfg['build_id'],
                          'hashes': hashes, 'ip_count': len(cfg['ips']),
                          'safe_read_count': sum(len(x['safe_reads']) for x in cfg['ips']),
                          'bitstream_validation': 'NOT_RUN', 'hwh_design_semantics': 'NOT_RUN',
                          'board_test': 'NOT_RUN'}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'FAIL', 'scope': 'offline_contract_and_files_only',
                          'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
