#!/usr/bin/env python3
"""Restore the Q1 checkpoint from the team's original archive or DCP file."""

import argparse
import hashlib
from pathlib import Path
import sys
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESTINATION_RELATIVE = Path(
    'V5.3-flash_PL01/reports/'
    'hardware_recovery_435f97f3_20261026_170641_ead3b9fa/final_routed.dcp'
)
ORIGINAL_ZIP_MEMBER = 'V5.3_FPGA_town_clean_20261030/' + DESTINATION_RELATIVE.as_posix()
EXPECTED_SHA256 = '8ffa9bda91d1fcc2bd83740546bad00b180237bee3be1a8472df3f82862b3c81'
MAX_SOURCE_BYTES = 64 * 1024 * 1024


def read_source(source):
    source = Path(source)
    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            matches = [item for item in archive.infolist() if item.filename == ORIGINAL_ZIP_MEMBER]
            if len(matches) != 1 or matches[0].is_dir():
                raise ValueError('ZIP must contain exactly one original Q1 DCP at the expected path')
            if matches[0].file_size > MAX_SOURCE_BYTES:
                raise ValueError('DCP source is too large')
            return archive.read(matches[0])
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('DCP source is too large')
    return source.read_bytes()


def restore(source):
    root = PROJECT_ROOT.resolve()
    destination = root / DESTINATION_RELATIVE
    parent = destination.parent.resolve()
    if not parent.is_relative_to(root):
        raise ValueError('DCP destination must remain inside the project')
    if not parent.is_dir():
        raise FileNotFoundError('Q1 hardware report directory is missing')
    if destination.is_symlink():
        raise FileExistsError('DCP destination is a symbolic link')
    if destination.exists():
        if not destination.is_file() or hashlib.sha256(destination.read_bytes()).hexdigest() != EXPECTED_SHA256:
            raise FileExistsError('DCP destination already exists with different contents')
        return 'already present'

    data = read_source(source)
    if hashlib.sha256(data).hexdigest() != EXPECTED_SHA256:
        raise ValueError('DCP SHA-256 does not match the original Q1 checkpoint')

    created = False
    try:
        with destination.open('xb') as output:
            created = True
            output.write(data)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise
    return 'restored'


def main():
    parser = argparse.ArgumentParser(
        description='Restore the exact Q1 checkpoint from the original project ZIP or a DCP file.')
    parser.add_argument('source', type=Path, help='Original project ZIP or original final_routed.dcp')
    args = parser.parse_args()
    try:
        result = restore(args.source)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print('DCP restore failed: {}'.format(error), file=sys.stderr)
        return 1
    print('DCP {}: {}'.format(result, DESTINATION_RELATIVE.as_posix()))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
