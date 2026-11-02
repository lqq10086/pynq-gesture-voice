"""Reuse existing credentials and verified SSH host keys; never package credentials."""
import argparse
import json
import shlex
import sys
from pathlib import Path, PurePosixPath
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
REMOTE = '/home/xilinx/jupyter_notebooks/V5.1_30p'
from board_transport import connect, execute
import paramiko

load_keys = paramiko.SSHClient.load_system_host_keys
paramiko.SSHClient.load_system_host_keys = lambda self, filename=None: load_keys(
    self, filename or str(ROOT/'scripts/known_hosts'))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--command')
    p.add_argument('--file', type=Path)
    p.add_argument('--sudo', action='store_true')
    p.add_argument('--save', type=Path)
    p.add_argument('--upload', nargs=2, action='append', default=[])
    p.add_argument('--download', nargs=2, action='append', default=[])
    a = p.parse_args()
    with connect() as client:
        with client.open_sftp() as sftp:
            for source, target in a.upload:
                remote_path = PurePosixPath(target)
                if (not target.startswith(REMOTE+'/') or '..' in remote_path.parts
                        or remote_path.as_posix() != target or '\\' in target):
                    raise ValueError('Uploads must stay in independent V5.1_30p root')
                parent = remote_path.parent.as_posix()
                code, out = execute(client, 'mkdir -p '+shlex.quote(parent))
                if code:
                    raise RuntimeError(out)
                sftp.put(source, target)
            for source, target in a.download:
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                sftp.get(source, target)
        command = a.command
        if a.file:
            command = '/usr/local/share/pynq-venv/bin/python3 -B -c '+shlex.quote(a.file.read_text(encoding='utf-8'))
        if command:
            if a.sudo:
                command = 'sudo -n env XILINX_XRT=/usr '+command
            code, out = execute(client, command)
            if a.save:
                a.save.write_text(out, encoding='utf-8')
            print(out)
            return code
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
