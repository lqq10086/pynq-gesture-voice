##############################################################################
# 一键搭起一个工程骨架：复制目录、写环境清单、可选地建 Vivado 工程。
# 命令：python tools/bootstrap_project.py --dest D:\proj\gesture --name gesture_pl ^
#         --part xc7z020clg400-2 [--create-vivado-project] [--force]
# 说明：它只创建目录与占位文件，不生成任何设计内容；写完把 NEXT 打印出来。
# 判据：退出码 0 表示骨架与 env.md 都写成功；3 表示缺 EDA 工具（骨架仍会建）；
#       4 表示目标目录已存在且没加 --force。
# 失效条件：目标目录里已有同名文件时不会覆盖；加 --force 会覆盖同名模板文件。
##############################################################################
"""按模板搭一个 FPGA/PYNQ 工程骨架。"""

import argparse
import json
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

HERE = Path(__file__).resolve().parent
SKILL2 = HERE.parent
TEMPLATE = SKILL2 / "work_sample"
EDA_ENV = SKILL2.parent / "eda-script-runner" / "tools" / "eda_env.py"

DIRS = ["src/rtl", "src/hls", "src/constraints", "sim/tb", "build/out", "tests",
        "board", "docs", "data/input", "data/golden", "report", "skill"]


def env_report() -> tuple[str, int]:
    """调用 skill/3 的探测脚本，返回 (要写进 env.md 的文本, 退出码)。"""
    if not EDA_ENV.exists():
        return ("未找到 tools/eda_env.py（它属于同级的 eda-script-runner）。\n"
                "这是缺依赖，不是可以跳过的一步：把缺什么报给上级，不要凭印象写工具版本。\n", 3)
    proc = subprocess.run([sys.executable, str(EDA_ENV), "--json"],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ("工具探测输出不是 JSON，原文如下：\n" + proc.stdout + proc.stderr, proc.returncode)
    lines = ["# 环境清单", "",
             "由 `tools/bootstrap_project.py` 调用 `eda-script-runner/tools/eda_env.py` 生成。",
             "换机器后重跑一次，不要沿用这份数字。", "",
             "| 工具 | 路径 | 版本 |", "| --- | --- | --- |"]
    for name, item in (data.get("tools") or {}).items():
        if item.get("path"):
            lines.append(f"| {name} | `{item['path']}` | {item.get('version') or '未探测'} |")
        else:
            lines.append(f"| {name} | 未找到 | - |")
    if data.get("missing"):
        lines += ["", "缺失：" + "、".join(data["missing"])]
    lines += ["", f"探测状态：{data.get('status')}", ""]
    return ("\n".join(lines), proc.returncode)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dest", default="", help="工程目录，不存在时创建")
    parser.add_argument("--name", default="", help="工程名，写进 README 与 env.md")
    parser.add_argument("--part", default="", help="器件型号，例如 xc7z020clg400-2")
    parser.add_argument("--force", action="store_true", help="目标已存在时覆盖模板文件")
    parser.add_argument("--create-vivado-project", action="store_true",
                        help="顺手调用 skill/3 的 create_hardware_project.tcl")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            rc = main(["--dest", str(Path(tmp) / "proj"), "--name", "selftest"])
            ok = rc == 0 and (Path(tmp) / "proj" / "docs" / "env.md").exists()
            print(("PASS " if ok else "FAIL ") + "自检：能建出骨架与 env.md")
            again = main(["--dest", str(Path(tmp) / "proj"), "--name", "selftest"])
            print(("PASS " if again == 4 else "FAIL ") + "自检：目录已存在时返回 4")
            return 0 if ok and again == 4 else 1

    if not args.dest:
        print("MISSING_INPUT: --dest 传目标工程目录")
        return 2
    dest = Path(args.dest).expanduser().resolve()
    if dest.exists() and any(dest.iterdir()) and not args.force:
        print(f"目标目录非空：{dest}")
        print("NEED: 换一个目录，或加 --force 覆盖模板文件（不会删你的 src/ 代码）")
        return 4
    if not TEMPLATE.exists():
        print(f"MISSING_INPUT: 模板目录不存在 {TEMPLATE}")
        return 2

    dest.mkdir(parents=True, exist_ok=True)
    for d in DIRS:
        (dest / d).mkdir(parents=True, exist_ok=True)

    copied = []
    for src in sorted(TEMPLATE.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(TEMPLATE)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not args.force:
            continue
        shutil.copyfile(src, target)
        copied.append(str(rel))

    env_text, env_rc = env_report()
    (dest / "docs" / "env.md").write_text(env_text, encoding="utf-8")

    readme = dest / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        text = text.replace("work_sample", args.name or dest.name)
        if args.part:
            text = text.replace("xc7z020clg400-2", args.part)
        readme.write_text(text, encoding="utf-8")

    print(f"骨架已建：{dest}")
    print(f"模板文件 {len(copied)} 个；环境清单 docs/env.md")
    print("NEXT:")
    print("  1. 填 docs/requirements.docx 与 docs/interface.docx，再动 src/")
    print("  2. 改 src/constraints/*.xdc 里的引脚与时钟，别照抄")
    step = 3
    runner = EDA_ENV.parent / "run_tcl.py"
    creator = SKILL2.parent / "eda-script-runner" / "vivado" / "create_hardware_project.tcl"
    if args.create_vivado_project:
        if runner.exists() and creator.exists() and args.part:
            cmd = [sys.executable, str(runner), str(creator), "--args",
                   f"-part {args.part} -project_dir {dest / 'build' / 'proj'} "
                   f"-project_name {args.name or dest.name} -rtl_dir {dest / 'src' / 'rtl'}"]
            print("  " + " ".join(cmd))
            subprocess.run(cmd, check=False)
        else:
            print(f"  3. （未建 Vivado 工程：需要 --part，且 {runner} 与 {creator} 必须存在）")
    else:
        print(f"  {step}. 建工程：python {runner} {creator} "
              f"--args \"-part {args.part or '<part>'} -project_dir {dest / 'build' / 'proj'} "
              f"-project_name {args.name or dest.name} -rtl_dir {dest / 'src' / 'rtl'}\"")
        step += 1
    print(f"  {step}. 构建与门禁：python {runner} "
          f"{SKILL2.parent / 'eda-script-runner' / 'vivado' / 'build_hardware.tcl'} "
          f"--args \"-project_dir {dest / 'build' / 'proj'} -project_name {args.name or dest.name}\"")
    return 0 if env_rc == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
