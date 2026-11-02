#!/usr/bin/env python3
"""统一入口：定位工具、检查前置条件、跑 Tcl 脚本、把结论翻译成机器可读的结果。

它存在的理由：直接敲 `vivado -mode batch -source x.tcl` 时，换一台机器就会出现三种
没有信息量的失败——找不到 vivado、脚本路径不对、缺输入文件。这个包装器把三种情况
分别说清楚，并把"缺什么"变成可以往上转交的结论，而不是一句 traceback。

用法：
    python run_tcl.py vivado/build_hardware.tcl --args "myproj 4"
    python run_tcl.py vivado/report_timing_paths.tcl --tool vivado --args "reports/impl"
    python run_tcl.py hls/build_ip.tcl --tool hls --dry-run
    python run_tcl.py --self-test

退出码：
    0 脚本正常结束
    2 工具正常启动但脚本报错（rc != 0）
    3 环境不满足（工具未找到），结论里有 need 列表
    4 前置条件不满足（脚本或输入文件不存在）
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="mbcs", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import eda_env  # noqa: E402
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

TOOL_CMDS = {
    "vivado": lambda exe, script, args: [exe, "-mode", "batch", "-notrace", "-source", script, "-tclargs", *args],
    "hls": lambda exe, script, args: [exe, "-f", script, *args],
    "vitis": lambda exe, script, args: [exe, "-mode", "batch", "-source", script, "-tclargs", *args],
    "xsct": lambda exe, script, args: [exe, script, *args],
}


def emit(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status: {report['status']}")
        for k, v in report.items():
            if k in {"status", "tail"}:
                continue
            print(f"  {k}: {v}")
        if report.get("tail"):
            print("  最后输出：")
            for line in report["tail"]:
                print("    " + line)


def decode_console(data):
    """工具输出编码不固定：Vivado 自己的消息是 ASCII，脚本里 puts 出去的中文
    可能按 UTF-8 也可能按控制台代码页（GBK）输出，而且管道截断时会切在多字节中间。
    先宽松按 UTF-8 解，坏字符多再退回 mbcs，取坏字符少的那种。"""
    if not data:
        return ""
    u = data.decode("utf-8", errors="replace")
    if u.count("\ufffd") <= 2:
        return u
    try:
        g = data.decode("mbcs", errors="replace")
    except LookupError:
        return u
    return g if g.count("\ufffd") < u.count("\ufffd") else u


def tcl_safe_args(args):
    """Windows 路径里的反斜杠会被 Tcl 当转义符吃掉（\\r \\t \\b 尤甚），统一换成斜杠。
    只处理看起来像 Windows 路径的参数，其它参数原样转发。"""
    out = []
    for a in args:
        looks_like_path = (len(a) > 2 and a[1] == ":") or a.startswith("\\\\")
        out.append(a.replace("\\", "/") if looks_like_path else a)
    return out


def warn_non_ascii(*paths: str) -> None:
    """Vivado/XSCT 的批处理入口按本地编码解析路径，含中文时常见报错是
    TclStackFree: incorrect freePtr 或路径乱码，而不是清晰的「路径不合法」。
    这里提前告警，让人知道该把工程放到纯英文路径下。"""
    bad = [p for p in paths if p and not p.isascii()]
    if bad:
        print("WARNING: 路径含非 ASCII 字符，Vivado/XSCT 可能在批处理里报 TclStackFree 或乱码：")
        for p in bad:
            print("  - " + p)
        print("WARNING: 建议把工程放到纯英文路径（例如 D:/proj）后重试。")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("script", nargs="?", help="相对当前目录或 eda-script-runner 根目录的路径，或绝对路径")
    ap.add_argument("--tool", default=None, choices=sorted(TOOL_CMDS), help="缺省按脚本目录推断")
    ap.add_argument("--args", default="", help="传给 -tclargs 的参数，空格分隔")
    ap.add_argument("--cwd", default=None, help="工作目录，缺省为系统临时目录下的 eda-skill-cwd")
    ap.add_argument("--log", default=None, help="日志文件路径，缺省写到系统临时目录下的 eda-skill-logs")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ns = ap.parse_args(argv[1:])

    if ns.self_test:
        return self_test()

    if not ns.script:
        print(__doc__)
        return 4

    script = Path(ns.script)
    if not script.is_absolute():
        candidates = [Path.cwd() / script, (HERE.parent / script)]
        found = next((c.resolve() for c in candidates if c.is_file()), None)
        script = found if found else candidates[0].resolve()
    if not script.is_file():
        emit({"status": "MISSING_INPUT", "script": str(script),
              "need": [f"脚本文件 {script}"],
              "hint": "使用相对当前目录或 eda-script-runner 根目录的路径，也可以传绝对路径。"}, ns.json)
        return 4

    args_raw = ns.args.split() if ns.args else []
    warn_non_ascii(str(script), ns.cwd or str(script.parent), *args_raw)
    tool = ns.tool or infer_tool(script)
    report = eda_env.discover(only=tool)
    if report["status"] != "FOUND":
        emit({"status": "MISSING_ENV", "tool": tool, "need": report["need"],
              "hint": "取得工具路径后设置对应环境变量或 PATH，再重跑。"}, ns.json)
        return 3

    exe = report["tools"][tool]["path"]
    args = ns.args.split()
    args = tcl_safe_args(args)
    cmd = TOOL_CMDS[tool](exe, str(script), args)
    cwd = Path(ns.cwd).resolve() if ns.cwd else (Path(tempfile.gettempdir()) / "eda-skill-cwd")
    cwd.mkdir(parents=True, exist_ok=True)
    if ns.dry_run:
        emit({"status": "DRY_RUN", "tool": tool, "exe": exe, "version": report["tools"][tool]["version"],
              "cmd": " ".join(cmd), "cwd": str(cwd)}, ns.json)
        return 0

    if ns.log:
        log_path = Path(ns.log)
    else:
        # 日志放系统临时目录：脚本目录要保持干净，别人 clone 下来不该多出运行产物
        log_dir = Path(tempfile.gettempdir()) / "eda-skill-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"run-{script.stem}-{tool}.log"
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True,
                              timeout=ns.timeout)
    except subprocess.TimeoutExpired:
        emit({"status": "TIMEOUT", "tool": tool, "timeout_s": ns.timeout}, ns.json)
        return 2

    out_text = decode_console(proc.stdout)
    err_text = decode_console(proc.stderr)
    log_path.write_text(out_text + "\n--- stderr ---\n" + err_text, encoding="utf-8")
    lines = (out_text + err_text).splitlines()
    tail = [l for l in lines if l.strip()][-25:]

    # 非零退出不可单凭缺少 ERROR 文本判断为成功，必须由调用方检查日志和产物。
    # 只有退出码为零时，CRITICAL WARNING 才降级为 WARN。
    text = out_text + err_text
    has_missing = "MISSING_INPUT:" in text
    has_incompatible = "TOOL_INCOMPATIBLE" in text
    has_error = ("ERROR:" in text or "Traceback (most recent call last)" in text
                 or "GATE_FAIL" in text)
    has_critical = "CRITICAL WARNING:" in text
    if has_missing:
        # 脚本自己声明缺输入时，包装脚本必须同样判 2。
        # 把「没跑」当成「跑成功了」是最难查的一类假进展。
        status = "MISSING_INPUT"
        hint = "把 log 里 NEED 行的内容转给上级补齐；这些值无法从工程骨架推断"
    elif has_incompatible:
        status = "TOOL_INCOMPATIBLE"
        hint = "工具版本不支持该流程；请查看日志中的版本要求"
    elif has_error or proc.returncode != 0:
        status = "SCRIPT_FAILED"
        hint = "检查工具退出码、日志和产物；时序或 DRC 不达标时查看 GATE_FAIL"
    elif has_critical:
        status = "WARN"
        hint = "工具退出码为 0；检查 CRITICAL WARNING 和产物后再判断结果"
    else:
        status = "OK"
        hint = ""
    emit({"status": status, "tool": tool, "version": report["tools"][tool]["version"],
          "returncode": proc.returncode, "log": str(log_path), "tail": tail, "hint": hint},
         ns.json)
    return 0 if status in ("OK", "WARN") else (3 if status == "TOOL_INCOMPATIBLE" else 2)


def infer_tool(script: Path) -> str:
    """按目录名、文件名和脚本内容判断该用哪个工具。

    只看文件名会把 build_software_vitis_classic.tcl 派给 vivado：
    vivado 里没有 setws/platform 命令，跑起来只会报 invalid command。
    """
    parts = {p.lower() for p in script.parts}
    name = script.name.lower()
    if "hls" in parts or name.startswith("build_ip") or name.startswith("cosim"):
        return "hls"
    try:
        text = script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        text = ""
    if "setws " in text or "platform create" in text or "connect -url" in text:
        return "xsct"
    if "csynth_design" in text or "cosim_design" in text:
        return "hls"
    if "xsct" in parts or name.startswith("delivery_board"):
        return "xsct"
    if "vitis" in parts or "vitis" in name:
        return "vitis"
    return "vivado"


def self_test() -> int:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + name)
        ok = ok and cond

    cmd = TOOL_CMDS["vivado"]("vivado.bat", "x.tcl", ["a", "b"])
    check("vivado 命令行形状", cmd[:6] == ["vivado.bat", "-mode", "batch", "-notrace", "-source", "x.tcl"])
    check("参数被转发", cmd[-2:] == ["a", "b"])
    check("xsct 不带 mode", TOOL_CMDS["xsct"]("xsct.bat", "d.tcl", ["jtag"]) == ["xsct.bat", "d.tcl", "jtag"])
    check("工具推断 hls", infer_tool(Path("eda-script-runner/hls/build_ip.tcl")) == "hls")
    check("工具推断 vivado", infer_tool(Path("eda-script-runner/vivado/build_hardware.tcl")) == "vivado")
    check("缺脚本给 4", main(["run_tcl.py", "no-such-file.tcl", "--json"]) == 4)

    original = eda_env.discover
    eda_env.discover = lambda probe=False, only=None: {"status": "NOT_FOUND", "tools": {},
                                                       "missing": [only], "need": [f"{only} 路径"]}
    try:
        rc = main(["run_tcl.py", "vivado/report_timing_paths.tcl", "--json"])
        check("缺工具给 3", rc == 3)
    finally:
        eda_env.discover = original
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
