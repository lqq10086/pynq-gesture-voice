"""Local native build only; does not connect to or execute on the board."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def build(arm=False):
    folder=ROOT/'board/build_pl_batch'/('armv7' if arm else 'host');folder.mkdir(parents=True,exist_ok=True)
    source=ROOT/'native/pl_batch.cpp'
    if os.name=='nt' and not arm:
        target=folder/'pl_batch.dll';batch=folder/'build.cmd';vcvars='C:/Program Files/Microsoft Visual Studio/2022/Community/VC/Auxiliary/Build/vcvars64.bat'
        batch.write_text('@echo off\ncall "'+vcvars+'" >nul\nif errorlevel 1 exit /b %errorlevel%\ncl /nologo /LD /O2 /EHsc /std:c++17 "'+str(source)+'" /link /OUT:"'+str(target)+'"\n')
        command=['cmd.exe','/d','/c',str(batch)]
    else:
        target=folder/'libpl_batch.so';compiler='g++'
        if arm:compiler='D:/Tools/FPGA/Vitis/2023.1/gnu/aarch32/nt/gcc-arm-linux-gnueabi/bin/arm-linux-gnueabihf-g++.exe'
        command=[compiler,'-O3','-std=c++11','-shared','-fPIC','-pthread',str(source),'-o',str(target)]
        if arm:command+=['-mcpu=cortex-a9','-mfpu=neon','-mfloat-abi=hard']
    result=subprocess.run(command,cwd=folder,text=True,encoding='utf-8',errors='replace',capture_output=True)
    (folder/'build.log').write_text(result.stdout+result.stderr)
    if result.returncode:raise RuntimeError(result.stdout+result.stderr)
    (folder/'build.json').write_text(json.dumps(dict(status='CROSS_BUILD_ONLY_TARGET_NOT_RUN' if arm else 'HOST_BUILD_ONLY',
        output=str(target),library_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        source_sha256={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (source,ROOT/'native/pl_batch.h')},
        command=command,board_executed=False),indent=2)+'\n')
    return target


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--arm',action='store_true');print(build(p.parse_args().arm))
