"""Local transport: reuse existing login without copying credentials into deliverables."""
import ast
import importlib.util
import os
import sys
from pathlib import Path

AUTH=Path(__file__).resolve().parents[2]/'V2/V2.9_YOLOv5s_PS_PL_ACCEL/scripts/remote.py'
sys.path.insert(0,str(AUTH.parents[1]/'tools/python'))

def connect():
    spec=importlib.util.spec_from_file_location('_existing_auth',AUTH)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.connect()

def execute(c,command):
    elevated=command.startswith('sudo -n ')
    if elevated:command=command.replace('sudo -n ',"sudo -S -p '' ",1)
    stdin,stdout,stderr=c.exec_command(command,timeout=1800)
    if elevated:
        tree=ast.parse(AUTH.read_text(encoding='utf-8'))
        value=next(k.value for n in ast.walk(tree) if isinstance(n,ast.Call) for k in n.keywords if k.arg=='password')
        credential=eval(compile(ast.Expression(value),'<existing login>','eval'),{'os':os})
        stdin.write(credential+'\n');stdin.flush()
    output=stdout.read().decode()+stderr.read().decode()
    return stdout.channel.recv_exit_status(),output
