# 用途：板上冒烟。加载 overlay，检查 IP 与基地址，并列出寄存器名称。
# 输入：--bit <路径>（缺省按本文件位置推 build/out/<工程名>/<工程名>.bit）。
# 输出：控制台报告；退出码 0 全部符合，2 有不一致，3 无法加载或缺输入。
# 工具版本：PYNQ 2.7 与 3.x。
# 换平台时按实际设计填写 EXPECTED_IP，并确认 IP 名称与地址。

import sys
from pathlib import Path
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent


def guess_bit() -> Path:
    """按工程骨架的目录约定找位流：build/out/<工程名>/<工程名>.bit。"""
    candidates = sorted((HERE.parent / "build" / "out").glob("*/*.bit"))
    return candidates[0] if candidates else HERE.parent / "build" / "out" / "<project>" / "<project>.bit"


# 期望的 IP 名与基地址，用来与 HWH 对齐。运行前必须填写
EXPECTED_IP = {
    # "my_ip_0": "0x43C00000",
}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bit = guess_bit()
    if "--bit" in argv:
        bit = Path(argv[argv.index("--bit") + 1])

    if "--dry-run" in argv:
        print(f"NOT_RUN: no board in this environment; would load {bit}")
        return 3

    if not EXPECTED_IP:
        print("MISSING_INPUT: fill EXPECTED_IP with the expected IP names and addresses")
        return 3

    try:
        from pynq import Overlay
    except ImportError:
        print("NOT_RUN: pynq not importable, run this on the board")
        return 3

    if not bit.exists():
        print(f"MISSING_INPUT: bitstream not found: {bit}")
        print("NEED: 传 --bit <路径>，或先跑 build 把位流放到 build/out/<工程名>/")
        return 3

    ol = Overlay(str(bit))
    print(f"loaded: {bit.name}")

    missing = [name for name in EXPECTED_IP if name not in ol.ip_dict]
    if missing:
        print(f"MISMATCH: missing ip {missing}")
        print(f"available: {sorted(ol.ip_dict)}")
        return 2

    address_mismatch = False
    for name, addr in EXPECTED_IP.items():
        got = ol.ip_dict[name]["phys_addr"]
        flag = "OK" if int(got) == int(addr, 16) else "MISMATCH"
        print(f"{flag:9s} {name:24s} addr={hex(got)} expected={addr}")
        if flag == "MISMATCH":
            address_mismatch = True

    if address_mismatch:
        return 2

    # 列出硬件描述中提供的寄存器名称。
    for name, info in sorted(ol.ip_dict.items()):
        if not info.get("registers"):
            continue
        print(f"registers of {name}: {sorted(info['registers'])}")

    print("RESULT: SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
