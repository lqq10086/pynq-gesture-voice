"""Stage outside the checkout; hold OwnerGuard from idle checks through final verification."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import sys
import tarfile
import tempfile
import types

PYTHON = '/usr/local/share/pynq-venv/bin/python3'


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _target(root, name):
    relative = PurePosixPath(name)
    if (not name or relative.is_absolute() or relative.as_posix() != name or
            any(part in ('', '.', '..') for part in relative.parts) or '\\' in name or ':' in name):
        raise ValueError('Unsafe deployment path: ' + name)
    target = root.joinpath(*relative.parts)
    if root not in target.resolve().parents:
        raise ValueError('Deployment path escapes checkout: ' + name)
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError('Deployment must not overwrite a symlink: ' + name)
        current = current.parent
    return target


def _stage_payload(payload, stage):
    names = set()
    with tarfile.open(payload, 'r:gz') as archive:
        for member in archive:
            target = _target(stage, member.name)
            if not member.isfile() or member.name in names:
                raise ValueError('Deployment accepts unique regular files only: ' + member.name)
            names.add(member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open('xb') as output:
                shutil.copyfileobj(source, output)
    manifest = json.loads((stage / 'deploy_manifest.json').read_text())
    if not isinstance(manifest, dict) or not manifest or 'deploy_manifest.json' in manifest:
        raise ValueError('Invalid deployment manifest')
    for name, entry in manifest.items():
        _target(stage, name)
        if (not isinstance(entry, dict) or type(entry.get('bytes')) is not int or entry['bytes'] < 0 or
                not isinstance(entry.get('sha256'), str) or len(entry['sha256']) != 64 or
                any(c not in '0123456789abcdef' for c in entry['sha256'])):
            raise ValueError('Invalid deployment identity: ' + name)
    changed = names - {'deploy_manifest.json'}
    if not changed <= set(manifest):
        raise ValueError('Archive contains files absent from its manifest')
    return manifest, sorted(changed)


def _verify_files(manifest, root, fallback=None):
    for name, entry in manifest.items():
        path = _target(root, name)
        if not path.is_file() and fallback is not None:
            path = _target(fallback, name)
        if not path.is_file() or path.stat().st_size != entry['bytes'] or _digest(path) != entry['sha256']:
            raise ValueError('Deployment byte/hash mismatch: ' + name)


def _validate_package(root):
    # Load the already hash-checked candidate contract afresh: do not reuse a
    # destination module cached before files were replaced.
    source = root / 'runtime/package_contract.py'
    contract = types.ModuleType('deployment_package_contract')
    contract.__file__ = str(source)
    exec(compile(source.read_bytes(), str(source), 'exec'), contract.__dict__)
    contract.validate_package(root / 'model_package')


def _publish_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    # Stage may be on another filesystem. Replace only a same-directory temp
    # file so every published file, including the manifest, contains full bytes.
    descriptor, temporary_file = tempfile.mkstemp(prefix='.deploy-', dir=str(target.parent))
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary_file)
        os.replace(temporary_file, target)
    finally:
        if os.path.exists(temporary_file):
            os.unlink(temporary_file)


def deploy_archive(payload, destination, expected_sha256, guard):
    payload, destination = Path(payload).resolve(), Path(destination).resolve()
    if destination == payload.parent or destination in payload.parents:
        raise ValueError('Payload must be staged outside the deployment destination')
    with guard.deployment_lock():
        if _digest(payload) != expected_sha256:
            raise ValueError('Uploaded deployment archive SHA256 mismatch')
        # Extract and verify every source byte before modifying any checkout file.
        with tempfile.TemporaryDirectory(prefix='verified-', dir=str(payload.parent)) as temporary:
            stage = Path(temporary).resolve()
            manifest, changed = _stage_payload(payload, stage)
            _verify_files(manifest, stage, fallback=destination)
            # Check every destination path before the first overwrite as well.
            for name in list(manifest) + ['deploy_manifest.json']:
                _target(destination, name)
            # Incremental archives omit unchanged package files. Complete only
            # this package/contract staging subset from the hash-verified target
            # so semantic package rejection also precedes every target write.
            for name in manifest:
                if name.startswith('model_package/') or name == 'runtime/package_contract.py':
                    target = _target(stage, name)
                    if not target.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(_target(destination, name), target)
            _validate_package(stage)
            for name in changed:
                _publish_file(stage / name, _target(destination, name))
            _verify_files(manifest, destination)
            _validate_package(destination)
            _publish_file(stage / 'deploy_manifest.json', _target(destination, 'deploy_manifest.json'))
            return dict(status='REMOTE_DEPLOYMENT_HASH_PASS', files=len(manifest),
                        changed_files=len(changed), errors=[], lifecycle_lock='HELD_THROUGH_VERIFICATION',
                        physical_gesture_acceptance='PENDING')


def _load_guard(source, expected_sha256):
    data = Path(source).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError('Staged OwnerGuard SHA256 mismatch')
    guard = types.ModuleType('deployment_owner_guard')
    guard.__file__ = str(source)
    exec(compile(data, str(source), 'exec'), guard.__dict__)
    return guard


def deploy_remote(client, execute, root, remote, payload):
    """Use a verified standalone runner, never a runner being overwritten in place."""
    root, payload = Path(root), Path(payload)
    code, output = execute(client, 'mktemp -d /tmp/ax7020-v321-deploy.XXXXXXXX')
    if code:
        raise RuntimeError(output)
    stage = PurePosixPath(output.strip())
    if stage.parent != PurePosixPath('/tmp') or not stage.name.startswith('ax7020-v321-deploy.'):
        raise RuntimeError('Unexpected deployment staging directory')
    try:
        runner, owner = root / 'scripts/deployment_transaction.py', root / 'runtime/owner_guard.py'
        with client.open_sftp() as sftp:
            for source, name in [(payload, 'payload.tar.gz'), (runner, 'transaction.py'), (owner, 'owner_guard.py')]:
                sftp.put(str(source), str(stage / name))
        arguments = [str(stage / 'transaction.py'), '--payload', str(stage / 'payload.tar.gz'),
                     '--payload-sha256', _digest(payload), '--destination', remote,
                     '--owner-source', str(stage / 'owner_guard.py'), '--owner-sha256', _digest(owner)]
        bootstrap = (
            'import hashlib,sys\nfrom pathlib import Path\n'
            'p=Path(%r);data=p.read_bytes()\n'
            'if hashlib.sha256(data).hexdigest()!=%r: raise RuntimeError("Deployment runner SHA256 mismatch")\n'
            'sys.argv=%r\nexec(compile(data,str(p),"exec"),dict(__name__="__main__",__file__=str(p)))\n'
        ) % (str(stage / 'transaction.py'), _digest(runner), arguments)
        return execute(client, 'sudo -n env XILINX_XRT=/usr ' + PYTHON + ' -B -c ' + shlex.quote(bootstrap))
    finally:
        # This path came from mktemp and was checked above; never remove a checkout.
        cleanup = 'import shutil;shutil.rmtree(%r)' % str(stage)
        code, output = execute(client, 'sudo -n ' + PYTHON + ' -B -c ' + shlex.quote(cleanup))
        if code:
            print('Deployment staging cleanup failed: ' + output, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', required=True)
    parser.add_argument('--payload-sha256', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--owner-source', required=True)
    parser.add_argument('--owner-sha256', required=True)
    args = parser.parse_args()
    guard = _load_guard(args.owner_source, args.owner_sha256)
    report = deploy_archive(args.payload, args.destination, args.payload_sha256, guard)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
