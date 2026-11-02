##############################################################################
# 生成 docs 与 report 下的 Word 模板（.docx）。模板本身随包提供，
# 只有在要改格式时才需要重跑这一个脚本。
# 命令：python tools/make_templates.py [--out <工程骨架目录>]
# 字体约定：中文标题黑体、正文宋体；拉丁字符 Times New Roman；小四 10.5 pt、1.5 倍行距。
# 判据：生成后能用 Word 打开且页数 ≥ 1；表格有框线。
##############################################################################
"""生成工程文档与报告模板。"""

import argparse
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    # keep Chinese readable when output is captured by another tool
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
SAMPLE = HERE.parent / "work_sample"

CN_BODY = "宋体"
CN_HEAD = "黑体"
LATIN = "Times New Roman"


def _font(run, cn=CN_BODY, size=10.5, bold=False, gray=False):
    run.font.name = LATIN
    run.font.size = Pt(size)
    run.bold = bold
    if gray:
        run.font.color.rgb = RGBColor(0x59, 0x59, 0x59)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), cn)
    rfonts.set(qn("w:ascii"), LATIN)
    rfonts.set(qn("w:hAnsi"), LATIN)


def new_doc():
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.left_margin = Cm(3.2)
    section.right_margin = Cm(3.2)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    style = doc.styles["Normal"]
    style.font.name = LATIN
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), CN_BODY)
    return doc


def title(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _font(p.add_run(text), cn=CN_HEAD, size=16, bold=True)
    return p


def heading(doc, text, level=1):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(4)
    _font(p.add_run(text), cn=CN_HEAD, size=12 if level == 1 else 11, bold=True)
    return p


def hint(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    _font(p.add_run(text), size=9, gray=True)
    return p


def para(doc, text):
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = Cm(0.74)
    p.paragraph_format.line_spacing = 1.5
    _font(p.add_run(text))
    return p


def table(doc, headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.style = "Table Grid"
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        _font(cell.paragraphs[0].add_run(h), cn=CN_HEAD, size=9, bold=True)
    for row in rows:
        cells = t.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = ""
            _font(cells[i].paragraphs[0].add_run(value or ""), size=9)
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    return t


def save(doc, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path



def md_to_docx(md_path: Path, out_path: Path):
    """把 reports/*.md 模板转成同样排版的 docx。"""
    lines = md_path.read_text(encoding="utf-8-sig").splitlines()
    doc = new_doc()
    i = 0
    in_bullet = False
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip().lstrip("\ufeff")
        if not line.strip():
            in_bullet = False
            i += 1
            continue
        if line.startswith("# "):
            title(doc, line[2:].strip())
            in_bullet = False
        elif line.startswith("## "):
            heading(doc, line[3:].strip(), level=1)
            in_bullet = False
        elif line.startswith("### "):
            heading(doc, line[4:].strip(), level=2)
            in_bullet = False
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(set(c) <= set("-: ") for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                table(doc, rows[0], rows[1:])
            continue
        elif line.startswith("- "):
            para(doc, "· " + _plain(line[2:]).replace(".md", ".docx"))
            in_bullet = True
        else:
            para(doc, _plain(line).replace(".md", ".docx"))
            in_bullet = False
        i += 1
    save(doc, out_path)


def _plain(text: str) -> str:
    """去掉 markdown 的强调与行内代码标记，保留原文。"""
    out = text.replace("**", "").replace("`", "")
    return out


def convert_reports() -> int:
    src = HERE.parent / "reports"
    made = 0
    for md in sorted(src.glob("*.md")):
        out = md.with_suffix(".docx")
        md_to_docx(md, out)
        print(f"{out.name}: {out.stat().st_size} B")
        made += 1
    return made


def docs_requirements(out: Path):
    doc = new_doc()
    title(doc, "需求与验收口径")
    hint(doc, "这份文件的作用：把「做什么、做到什么算过」写死，后面所有分工与验收都引它。"
              "只写下判断所需的信息，不写设计说明。写完先给另一个人读一遍，看他能否指出歧义。")
    heading(doc, "1 目标")
    para(doc, "一句话说明这个工程要让谁在什么场景下完成什么动作。避免出现「实现某某算法」这类无法验收的写法。")
    heading(doc, "2 验收场景")
    para(doc, "逐条列出可观察的验收场景，每条写成「给定什么输入 → 观察到什么 → 判定通过的条件」。"
              "场景要能由第二个人照着做一遍。")
    table(doc, ["编号", "输入/前置条件", "观察到的现象", "通过条件", "证据在哪"],
          [["A1", "", "", "", ""], ["A2", "", "", "", ""]], [1.6, 4.2, 4.2, 3.4, 2.2])
    heading(doc, "3 明确的非目标")
    para(doc, "写清这次不做的事。这一节比目标更能防止范围膨胀。")
    heading(doc, "4 硬约束")
    para(doc, "器件型号、工具版本、时钟上限、内存上限、交付时间、允许改动的文件范围。"
              "约束写成可检查的形式，例如「只允许改 src/ 与 sim/」，而不是「尽量少改」。")
    heading(doc, "5 判定口径")
    para(doc, "说明数字怎么取：计时从哪一刻到哪一刻、要不要预热、取中位数还是最小值、样本量多少、"
              "哪些指标只在本机成立。口径不同的两个数字不许放在一张表里比较。")
    return save(doc, out / "docs" / "requirements.docx")


def docs_interface(out: Path):
    doc = new_doc()
    title(doc, "接口契约")
    hint(doc, "这份文件是主机侧与硬件侧共同的唯一事实来源。每个地址、每个字段都要能追到源文件与行号；"
              "从手册抄来的值要注明是哪一版手册。")
    heading(doc, "1 接口清单")
    table(doc, ["接口", "方向", "位宽/带宽", "时钟域", "用途"],
          [["AXI4-Lite 控制", "PS → PL", "32 bit", "", "命令与状态"],
           ["AXI4-Stream 数据", "PL → PS", "", "", ""]], [3.0, 2.6, 2.6, 2.6, 4.0])
    heading(doc, "2 寄存器表")
    para(doc, "逐字段写：偏移、名称、访问方式、复位值、含义、副作用。有自清零位、读清位、写一次位的必须标出来，"
              "否则使用者一定会重复写而不知情。")
    table(doc, ["偏移", "名称", "访问", "复位值", "含义", "副作用"],
          [["0x00", "CONTROL", "读写", "", "", ""], ["0x04", "STATUS", "只读", "", "", ""]],
          [1.8, 3.0, 1.8, 2.0, 4.0, 3.0])
    heading(doc, "3 数据通路")
    para(doc, "写清缓冲区的数量、大小、地址、所有权与生命周期；谁分配、谁释放、什么时候可以写。")
    heading(doc, "4 时序与超时")
    para(doc, "一次命令从发出到完成的时序；硬件与软件各自的超时；超时后硬件进入什么状态、软件应该怎么处理。")
    heading(doc, "5 版本与身份")
    para(doc, "登记必须一起使用的文件与哈希：比特流、硬件描述、模型、解码器源码。"
              "任何一项换掉都要重新做一致性检查。")
    heading(doc, "6 未定项")
    para(doc, "把还没定的地方明确写出来，并写谁在什么时候定。未定项不要用默认值糊过去。")
    return save(doc, out / "docs" / "interface.docx")


def docs_decisions(out: Path):
    doc = new_doc()
    title(doc, "决策记录")
    hint(doc, "一条决策一行。重点写「为什么不选另一个」和「什么证据支持这个选择」；"
              "只写结论的记录，三个月后没人能用。")
    heading(doc, "1 记录格式")
    para(doc, "编号、日期、问题、候选方案、选择、理由、证据、回滚方式。没有任何一条可以省。")
    heading(doc, "2 决策表")
    table(doc, ["编号", "日期", "问题", "选择", "理由", "证据", "回滚"],
          [["D01", "", "", "", "", "", ""], ["D02", "", "", "", "", "", ""]],
          [1.4, 1.8, 3.4, 2.6, 4.0, 2.4, 2.0])
    return save(doc, out / "docs" / "decisions.docx")


def report_design(out: Path):
    doc = new_doc()
    title(doc, "设计报告")
    hint(doc, "给评审与接手者看。每个数字都要带测量条件；没做的写 NOT_RUN，不要用「基本完成」这类词。")
    heading(doc, "1 选题背景与创新点")
    para(doc, "")
    heading(doc, "2 设计原理与功能框图")
    para(doc, "框图里标清子模块与边界；每个箭头写明信号方向与位宽。")
    heading(doc, "3 软硬件划分依据")
    para(doc, "先给数据量与计算量，再给结论。例如每秒多少字节必须由 DMA 搬、多少乘加决定放在 PL。")
    heading(doc, "4 优化过程与前后对比")
    table(doc, ["方案", "做了什么", "资源变化", "性能变化", "代价", "是否采用"],
          [["基线", "", "", "", "", ""], ["候选 1", "", "", "", "", ""]],
          [2.4, 4.2, 2.8, 2.8, 2.8, 1.8])
    heading(doc, "5 功能正确性与验证")
    para(doc, "分层写：仿真、门级、板端数值、真实输入、人工操作。每层各自的证据与结论。")
    heading(doc, "6 未完成项与边界")
    para(doc, "把没做到的事、只在特定条件下成立的事写在这里。这一节不能为空。")
    return save(doc, out / "report" / "design-report.docx")


def report_optimization(out: Path):
    doc = new_doc()
    title(doc, "优化记录")
    hint(doc, "一条优化一张卡片，五件事缺一不可：前提、成本、风险、验证、回退。"
              "没有回退方式的改动不算优化，算赌博。")
    heading(doc, "优化卡片")
    table(doc, ["字段", "内容"],
          [["编号", ""], ["前提", "这个数字在什么输入、什么负载、什么计时边界下成立"],
           ["做了什么", ""], ["成本", "资源、工时、复杂度各自增加多少"],
           ["风险", "哪些指标可能变差，怎么发现"], ["验证", "用什么输入、比哪些量、判据是什么"],
           ["回退", "出问题怎么退回去，退回去要几分钟"], ["结论", "采用 / 不采用 / 待复测"]],
          [2.6, 11.4])
    heading(doc, "对照数据")
    table(doc, ["方案", "输入", "计时边界", "中位", "P95", "资源", "备注"],
          [["", "", "", "", "", "", ""]], [2.2, 2.8, 3.6, 1.8, 1.8, 2.4, 2.6])
    return save(doc, out / "report" / "optimization-log.docx")


def report_verification(out: Path):
    doc = new_doc()
    title(doc, "验证记录")
    hint(doc, "一条用例一行，写清层次。仿真通过不等于上板通过，上板通过不等于真实输入通过。")
    heading(doc, "用例表")
    table(doc, ["编号", "层次", "输入", "期望", "实际", "结论", "日志位置"],
          [["V01", "仿真", "", "", "", "", ""], ["V02", "板端数值", "", "", "", "", ""]],
          [1.4, 2.4, 3.2, 3.0, 3.0, 1.8, 2.6])
    heading(doc, "未覆盖项")
    para(doc, "列清哪些层次、哪些输入没有测到，以及为什么。")
    return save(doc, out / "report" / "verification-log.docx")


def report_gate(out: Path):
    doc = new_doc()
    title(doc, "硬件门禁记录")
    hint(doc, "工具跑完不等于可以部署。这一页只回答一个问题：这份比特流能不能上板。"
              "结论用布尔值写，不要写「应该没问题」。")
    heading(doc, "1 输入身份")
    table(doc, ["项", "值"],
          [["工具版本", ""], ["器件型号", ""], ["DCP 路径与哈希", ""], ["约束文件哈希", ""]],
          [3.4, 10.6])
    heading(doc, "2 时序")
    table(doc, ["指标", "值", "失败端点数", "判定"],
          [["WNS (ns)", "", "", ""], ["WHS (ns)", "", "", ""], ["WPWS (ns)", "", "", ""]],
          [3.0, 3.6, 3.6, 3.8])
    heading(doc, "3 资源")
    table(doc, ["资源", "占用", "容量", "占比"],
          [["LUT", "", "", ""], ["FF", "", "", ""], ["BRAM", "", "", ""], ["DSP", "", "", ""]],
          [3.0, 3.6, 3.6, 3.8])
    heading(doc, "4 结论")
    para(doc, "deployment_allowed = true / false。true 时写明允许到什么范围；false 时写清缺哪一项、下一步做什么。")
    return save(doc, out / "report" / "hardware-gate.docx")


def report_handoff(out: Path):
    doc = new_doc()
    title(doc, "交接说明")
    hint(doc, "接手的人应该能只读这份文件就把工程跑起来。凡是「你自己看一下就知道」的地方，都是这里该写的内容。")
    heading(doc, "1 环境")
    table(doc, ["项", "版本/路径", "在哪验证"],
          [["操作系统", "", ""], ["Vivado", "", ""], ["Python", "", ""], ["板卡与镜像", "", ""]],
          [3.2, 6.6, 4.2])
    heading(doc, "2 从零复现步骤")
    para(doc, "按顺序写命令，每一步写清预期输出。不要写「配置好环境后运行」这种句子。")
    heading(doc, "3 已知问题")
    para(doc, "当前不工作的部分、绕开的方式、以及绕开之后的代价。")
    heading(doc, "4 资产清单")
    table(doc, ["文件", "作用", "能否重新生成"],
          [["", "", ""]], [5.0, 6.0, 3.0])
    return save(doc, out / "report" / "handoff.docx")


BUILDERS = [docs_requirements, docs_interface, docs_decisions,
            report_design, report_optimization, report_verification,
            report_gate, report_handoff]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(SAMPLE), help="工程骨架根目录，默认 work_sample")
    parser.add_argument("--reports", action="store_true", help="convert reports templates")
    args = parser.parse_args()
    if args.reports:
        return 0 if convert_reports() else 1
    out = Path(args.out).resolve()
    for builder in BUILDERS:
        path = builder(out)
        print(f"WROTE {path.relative_to(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
