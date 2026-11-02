"""Read-only incremental deployment manifest check. No PYNQ or device imports."""
import argparse
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
def verify(root=ROOT,manifest=None):
    root=Path(root).resolve();path=Path(manifest or root/'reports/deployment_manifest.json')
    rows=json.loads(path.read_text());seen=set()
    if not isinstance(rows,list):raise ValueError('Deployment manifest must be a list')
    for row in rows:
        name=row['file'];target=(root/name).resolve()
        if root not in target.parents:raise ValueError('Payload path escaped version directory')
        if name in seen:raise ValueError('Duplicate deployment payload path')
        seen.add(name)
        if target.stat().st_size!=row['bytes'] or hashlib.sha256(target.read_bytes()).hexdigest()!=row['sha256']:raise ValueError('Payload mismatch: '+name)
    return dict(status='DEPLOYMENT_SHA256_PASS',files=len(rows),bytes=sum(r['bytes'] for r in rows))
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',type=Path);args=parser.parse_args();print(json.dumps(verify(manifest=args.manifest)))
if __name__=='__main__':main()
