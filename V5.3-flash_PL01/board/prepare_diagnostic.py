"""Freeze diagnostic sources only after real routed/model gates; no deployment."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import board_identity
from board.qualify_numeric import required_numeric_files


def freeze(root=ROOT):
    root=Path(root).resolve();camera=board_identity.verify_camera_overlay(root);package,offline=board_identity._offline(root)
    seed=json.loads((root/'reports/cpu_application_seed.json').read_text());files=set(seed['files'])|set(board_identity.CRITICAL_SOURCES)|set(offline)
    files.update(p.relative_to(root).as_posix() for p in board_identity.camera_module(root).bound_files(root))
    files.update(p.relative_to(root).as_posix() for p in (root/'board').glob('*.py'))
    files.update(p.relative_to(root).as_posix() for p in required_numeric_files(root))
    files.discard(board_identity.MANIFEST)
    result=dict(schema='pl01-diagnostic-source-v1',status='DIAGNOSTIC_SOURCE_FROZEN_NOT_BOARD',scope='PL01_OWNED_DIAGNOSTICS_ONLY',
        version=root.name,production_qualified=False,package_manifest_sha256=package,selection_sha256=camera['selection_sha256'],
        files={name:board_identity.sha(board_identity.local_path(root,name)) for name in sorted(files)})
    path=root/board_identity.DIAGNOSTIC_MANIFEST
    if path.exists():raise FileExistsError('preserve existing diagnostic source snapshot before requalification')
    path.write_text(json.dumps(result,indent=2)+'\n');return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--root',type=Path,default=ROOT);args=p.parse_args()
    try:result=freeze(args.root);print(json.dumps(dict(status=result['status'],files=len(result['files']))))
    except Exception as error:print(json.dumps(dict(status='BLOCKED_NOT_QUALIFIED',error=str(error))));raise SystemExit(2)
