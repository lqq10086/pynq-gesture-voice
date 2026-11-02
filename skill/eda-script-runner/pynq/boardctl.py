##############################################################################
# 通过 SSH 在 PYNQ 板卡上执行一段 Python 或命令，并按白名单上传下载文件。
# 命令：python pynq/boardctl.py --board <ip> --command "ls -l /home/xilinx"
#       python pynq/boardctl.py --board <ip> --file snippet.py --save out.log
#       python pynq/boardctl.py --board <ip> --download <远端文件> <本地文件>
# 来源：OV5640_COLOR_CAPTURE_20261018/scripts/boardctl.py，改写自原文件。
# 版本依赖：需要 paramiko；远端解释器默认 /usr/local/share/pynq-venv/bin/python3，
#           该路径在 PYNQ 2.7 与 3.x 镜像里都存在，可用 --python 覆盖。
# 与原文件的区别：原文件 `from board_transport import connect, execute` 依赖一个包里没有的模块，
#           直接运行会 ImportError；这里把连接与执行内联，板卡地址、账号、白名单根目录都改为参数或环境变量。
# 判据：它只负责搬运与执行，不判断结果对不对。远端非零退出码会原样带回来并以 1 退出。
# 失效条件：板卡没开 SSH、账号密码不对、白名单根目录与实际路径不符时直接报错，不会静默通过。
##############################################################################
"""SSH operations scoped to a whitelisted remote root."""

import argparse
import os
import shlex
import sys
from pathlib import Path, PurePosixPath
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

DEFAULT_PYTHON = '/usr/local/share/pynq-venv/bin/python3'


def connect(host, user, password, port=22, timeout=20):
    """返回 paramiko 连接；缺主机名时按 MISSING_INPUT 报错退出。"""
    if not host:
        print('MISSING_INPUT: 板卡地址：用 --board 或环境变量 PYNQ_BOARD 指定')
        sys.exit(2)
    try:
        import paramiko
    except ImportError:
        print('MISSING_INPUT: 缺少 paramiko，先 pip install paramiko')
        sys.exit(2)
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=user, password=password, timeout=timeout)
    return client


def execute(client, cmd):
    """执行一条命令，返回 (退出码, 合并后的输出)。"""
    _, stdout, stderr = client.exec_command(cmd)
    out = stdout.read().decode('utf-8', 'replace')
    err = stderr.read().decode('utf-8', 'replace')
    return stdout.channel.recv_exit_status(), out + err


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--board', default=os.environ.get('PYNQ_BOARD', ''))
    parser.add_argument('--user', default=os.environ.get('PYNQ_USER', 'xilinx'))
    parser.add_argument('--password', default=os.environ.get('PYNQ_PASSWORD', 'xilinx'))
    parser.add_argument('--port', type=int, default=22)
    parser.add_argument('--remote-root', default=os.environ.get('PYNQ_REMOTE_ROOT', '/home/xilinx'),
                        help='下载白名单根目录，默认 /home/xilinx')
    parser.add_argument('--python', default=DEFAULT_PYTHON)
    parser.add_argument('--command', help='在板上执行的 shell 命令')
    parser.add_argument('--file', type=Path, help='本地 Python 文件，内容在板上执行')
    parser.add_argument('--save', type=Path, help='把输出写到这个本地文件')
    parser.add_argument('--sudo', action='store_true', help='用 sudo -n 执行（需要免密）')
    parser.add_argument('--download', nargs=2, action='append', default=[], metavar=('REMOTE', 'LOCAL'))
    args = parser.parse_args()

    if not args.command and not args.file and not args.download:
        print('MISSING_INPUT: 至少要给 --command、--file 或 --download 之一')
        return 2

    client = connect(args.board, args.user, args.password, args.port)
    with client:
        with client.open_sftp() as sftp:
            for remote, local in args.download:
                remote_path = PurePosixPath(remote)
                if not remote_path.is_relative_to(PurePosixPath(args.remote_root)):
                    print('REFUSED: %s 不在白名单根 %s 下' % (remote, args.remote_root))
                    return 2
                Path(local).parent.mkdir(parents=True, exist_ok=True)
                sftp.get(remote, local)
                print('DOWNLOADED %s -> %s' % (remote, local))

        if args.file:
            source = args.file.read_text(encoding='utf-8-sig')
            cmd = args.python + ' -B -c ' + shlex.quote(source)
        else:
            cmd = args.command
        if args.save is None and not cmd:
            return 0

        if args.sudo:
            cmd = 'sudo -n env XILINX_XRT=/usr sh -c ' + shlex.quote(cmd)
        code, output = execute(client, cmd)
        if args.save:
            args.save.parent.mkdir(parents=True, exist_ok=True)
            args.save.write_text(output, encoding='utf-8')
            print('SAVED %s (%d bytes)' % (args.save, len(output)))
        else:
            print(output)
        if code:
            print('REMOTE_EXIT=%d' % code)
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
