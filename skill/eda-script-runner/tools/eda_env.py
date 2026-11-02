#!/usr/bin/env python3
"""定位本机的 AMD 工具链（Vivado / Vitis / Vitis HLS / xsct），找不到时说明缺什么。

设计原则：不假设安装路径，不假设版本，不在缺工具时抛异常。
找不到就输出结构化结论，告诉调用方（人或上级 agent）需要提供哪一条信息。

用法：
    python eda_env.py                     # 人类可读
    python eda_env.py --json              # 机器可读
    python eda_env.py --tool vivado       # 只关心某一个
    python eda_env.py --probe-version     # 额外跑一次 -version（慢，但版本确定）
    python eda_env.py --self-test

退出码：
    0 找到了请求的工具
    3 没找全（结论里给出 need 列表，调用方应向上要信息，而不是报错退出）
    2 脚本自身出错
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOOLS = {
    "vivado": {
        "exe": ["vivado.bat", "vivado"],
        "env": ["XILINX_VIVADO", "VIVADO_PATH"],
        "dirs": ["Vivado", "vivado"],
    },
    "vitis": {
        "exe": ["vitis.bat", "vitis"],
        "env": ["XILINX_VITIS"],
        "dirs": ["Vitis", "vitis"],
    },
    "hls": {
        "exe": ["vitis_hls.bat", "vitis_hls", "vivado_hls.bat", "vivado_hls"],
        "env": ["XILINX_HLS", "XILINX_VIVADO_HLS"],
        "dirs": ["Vitis_HLS", "Vivado_HLS", "vitis_hls"],
    },
    "xsct": {
        "exe": ["xsct.bat", "xsct"],
        "env": ["XILINX_VITIS", "XILINX_SDX"],
        "dirs": ["Vitis", "SDK"],
    },
}

DRIVE_SUBDIRS = [
    "Xilinx",
    "AMD",
    "vivado",
    "Vitis",
    "Tools",
    os.path.join("Tools", "FPGA"),
    os.path.join("Program Files", "Xilinx"),
    os.path.join("Program Files", "AMD"),
    os.path.join("Program Files (x86)", "Xilinx"),
]


def fixed_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    out = []
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append(root)
    return out


def candidate_roots() -> list[Path]:
    roots: list[Path] = []
    for var in ("XILINX_VIVADO", "XILINX_VITIS", "XILINX_HLS", "VIVADO_PATH", "XILINX_SDX"):
        val = os.environ.get(var)
        if val:
            roots.append(Path(val))
    for d in fixed_drives():
        for sub in DRIVE_SUBDIRS:
            p = Path(d) / sub
            if p.is_dir():
                roots.append(p)
    # 安装根上一层也可能直接叫 vivado / Vitis，补一层扫描
    for d in fixed_drives():
        try:
            for entry in os.scandir(d):
                if entry.is_dir() and entry.name.lower() in {"vivado", "vitis", "xilinx", "amd"}:
                    roots.append(Path(entry.path))
        except OSError:
            continue
    seen, uniq = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.is_dir():
            seen.add(key)
            uniq.append(r)
    return uniq


def find_exe(tool: str, roots: list[Path], max_depth: int = 4) -> dict | None:
    """在安装根下找 <root>/<版本>/bin/<exe> 之类的布局。"""
    spec = TOOLS[tool]
    names = [n.lower() for n in spec["exe"]]  # 保持顺序：Windows 下 .bat 优先

    def scan(base: Path, depth: int) -> Path | None:
        if depth > max_depth:
            return None
        try:
            entries = list(os.scandir(base))
        except OSError:
            return None
        for e in entries:
            if not e.is_dir(follow_symlinks=False):
                continue
            name = e.name.lower()
            if name == "bin":
                for exe in names:
                    p = Path(e.path) / exe
                    if p.is_file():
                        return p
                continue
            if name in {"data", "doc", "docnav", "downloads", "samples", "$recycle.bin"}:
                continue
            got = scan(Path(e.path), depth + 1)
            if got:
                return got
        return None

    for root in roots:
        hit = scan(root, 0)
        if hit:
            return {"path": str(hit)}
    return None


def from_path(tool: str) -> dict | None:
    for exe in TOOLS[tool]["exe"]:
        p = shutil.which(exe)
        if p:
            return {"path": p}
    return None


def derive_version(path: str) -> str:
    parts = Path(path).parts
    for part in reversed(parts[:-1]):
        if part.count(".") == 1 and part[0].isdigit():
            return part
    return "unknown"


def probe_version(path: str, timeout: int = 90) -> str:
    cmd = [path, "-version"] if path.lower().endswith(".bat") else [path, "-version"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=str(Path(path).parent))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"probe-failed: {exc.__class__.__name__}"
    text = (r.stdout or "") + (r.returncode * " " ) + (r.stderr or "")
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("vivado v") or line.lower().startswith("vitis") or line.lower().startswith("version"):
            return line
    return (r.stdout or "").strip().splitlines()[0] if (r.stdout or "").strip() else f"exit={r.returncode}"


def discover(probe: bool = False, only: str | None = None) -> dict:
    roots = candidate_roots()
    wanted = [only] if only else list(TOOLS)
    tools: dict[str, dict] = {}
    for tool in wanted:
        if tool not in TOOLS:
            continue
        hit = from_path(tool) or find_exe(tool, roots)
        if hit:
            ver = derive_version(hit["path"])
            rec = {"path": hit["path"], "version": ver, "version_source": "path"}
            if probe:
                rec["version_probe"] = probe_version(hit["path"])
                rec["version_source"] = "probe"
            tools[tool] = rec

    need = [t for t in wanted if t not in tools]
    status = "FOUND" if not need else ("PARTIAL" if tools else "NOT_FOUND")
    return {
        "status": status,
        "tools": tools,
        "missing": need,
        "searched_roots": [str(r) for r in roots],
        "need": [f"{t} 的可执行文件路径（例如 E:\\vivado\\Vivado\\2023.2\\bin\\vivado.bat）" for t in need],
        "notes": [
            "路径来自环境变量、PATH 或常见安装根；未做注册表查询。",
            "如果工具装在网络盘或自定义目录，请直接传入 --path 或设置对应环境变量。",
            "版本号默认从目录名推导，加 --probe-version 可用 -version 实测。",
        ],
    }


def human(report: dict) -> str:
    lines = [f"状态：{report['status']}"]
    for name, rec in report["tools"].items():
        extra = rec.get("version_probe")
        lines.append(f"  {name}: {rec['path']}  (版本 {rec['version']}，来源 {rec['version_source']})")
        if extra:
            lines.append(f"      实测：{extra}")
    if report["missing"]:
        lines.append("缺失：" + ", ".join(report["missing"]))
        lines.append("需要向上级确认的信息：")
        for item in report["need"]:
            lines.append(f"  - {item}")
        lines.append("在拿到路径之前，脚本不会假装能跑；把上面这段原样转给调用方即可。")
    else:
        lines.append("所需工具齐全，可以直接跑脚本。")
    if report["searched_roots"]:
        lines.append("已扫描的安装根：" + "；".join(report["searched_roots"][:12])
                     + ("……" if len(report["searched_roots"]) > 12 else ""))
    return "\n".join(lines)


def self_test() -> int:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    check("工具表覆盖四类", set(TOOLS) == {"vivado", "vitis", "hls", "xsct"})
    check("能枚举盘符", len(fixed_drives()) >= 1)
    check("版本推导", derive_version(r"E:\vivado\Vivado\2023.2\bin\vivado.bat") == "2023.2")
    check("版本推导容错", derive_version("/usr/bin/vivado") == "unknown")

    rep = discover(only="vivado")
    check("结构完整", set(rep) >= {"status", "tools", "missing", "need"})
    check("状态自洽", rep["status"] in {"FOUND", "PARTIAL", "NOT_FOUND"})
    if rep["tools"]:
        check("找到时不需要 need", not rep["need"])
    else:
        check("未找到时给出 need", bool(rep["need"]))

    rep2 = discover(only="not-a-tool")
    check("未知工具不崩", rep2["status"] == "NOT_FOUND")
    return 0 if ok else 2


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--tool", choices=sorted(TOOLS))
    ap.add_argument("--probe-version", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--path", help="直接指定可执行文件，跳过扫描")
    args = ap.parse_args(argv[1:])

    if args.self_test:
        return self_test()

    if args.path:
        p = Path(args.path)
        if not p.is_file():
            print(f"指定的路径不存在：{p}")
            print(f"NEED: {args.tool or '工具'} 的可执行文件路径")
            return 3
        rec = {"path": str(p), "version": derive_version(str(p)), "version_source": "argument"}
        if args.probe_version:
            rec["version_probe"] = probe_version(str(p))
        print(json.dumps({"status": "FOUND", "tools": {args.tool or "tool": rec},
                          "missing": [], "need": [], "searched_roots": [], "notes": []},
                         ensure_ascii=False, indent=2) if args.json else human(
            {"status": "FOUND", "tools": {args.tool or "tool": rec}, "missing": [],
             "need": [], "searched_roots": [], "notes": []}))
        return 0

    report = discover(probe=args.probe_version, only=args.tool)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(human(report))
    return 0 if report["status"] == "FOUND" else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv))

