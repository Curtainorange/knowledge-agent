"""把 分析设计文档.md 排版成 Word。

排版约定（中文技术文档）：
- A4、页边距 2.5cm；正文宋体 10.5pt / 西文 Times New Roman，行距 1.5
- 标题黑体：一级 15.5pt、二级 13pt、三级 11.5pt，颜色 #1F2D3D
- 表格 Table Grid，表头浅灰底 + 加粗 + 跨页重复，正文 9pt
- 图片居中，宽度取自然尺寸（200dpi）且不超过版面宽 15.5cm；图注 9pt 灰色居中
- 引用块左侧竖线；代码块等宽 + 浅底
"""
from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

BASE = Path(__file__).resolve().parent
SRC = BASE / "分析设计文档.md"
OUT = BASE / "分析设计文档.docx"

INK = RGBColor(0x1F, 0x2D, 0x3D)
MUTED = RGBColor(0x5D, 0x6D, 0x7E)
RULE = "B3BECB"
HEAD_FILL = "F2F5F8"
CODE_FILL = "F7F8FA"

MAX_W_CM = 15.5
MAX_H_CM = 16.5


# ---------- 基础工具 ----------
def set_run_font(run, *, size=None, bold=None, color=None, mono=False, italic=None, east_font=None):
    ascii_font = "Consolas" if mono else "Times New Roman"
    east = "Consolas" if mono else (east_font or "宋体")
    run.font.name = ascii_font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.append(rfonts)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)
    rfonts.set(qn("w:eastAsia"), east)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold
    if italic is not None:
        run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color


def p_border(paragraph, edge="left", size=12, color=RULE, space=8):
    ppr = paragraph._p.get_or_add_pPr()
    pbdr = ppr.find(qn("w:pBdr"))
    if pbdr is None:
        pbdr = OxmlElement("w:pBdr")
        ppr.append(pbdr)
    el = OxmlElement(f"w:{edge}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color)
    pbdr.append(el)


def shade_cell(cell, fill):
    tcpr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    tcpr.append(shd)


def shade_paragraph(paragraph, fill):
    ppr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), fill)
    ppr.append(shd)


def cell_margins(table, left=90, right=90, top=40, bottom=40):
    tblpr = table._tbl.tblPr
    mar = OxmlElement("w:tblCellMar")
    for tag, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{tag}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tblpr.append(mar)


def repeat_header(row):
    trpr = row._tr.get_or_add_trPr()
    el = OxmlElement("w:tblHeader")
    el.set(qn("w:val"), "true")
    trpr.append(el)


def add_page_field(paragraph):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.append(begin)
    run._r.append(instr)
    run._r.append(end)
    set_run_font(run, size=9, color=MUTED)


# ---------- 行内格式 ----------
INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


def add_inline(paragraph, text, *, size=10.5, color=INK, bold=False, mono_size=None, east_font=None):
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            set_run_font(run, size=size, bold=True, color=color, east_font=east_font)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            set_run_font(run, size=mono_size or size - 0.5, color=color, mono=True)
        else:
            run = paragraph.add_run(piece)
            set_run_font(run, size=size, color=color, bold=bold, east_font=east_font)


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(33)
    return struct.unpack(">II", head[16:24])


# ---------- 文档骨架 ----------
def setup_document() -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    for attr in ("left_margin", "right_margin"):
        setattr(sec, attr, Cm(2.5))
    sec.top_margin = Cm(2.5)
    sec.bottom_margin = Cm(2.3)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.5
    normal.paragraph_format.space_after = Pt(4)

    for name, size, before, after in (
        ("Heading 1", 15.5, 18, 10),
        ("Heading 2", 13, 14, 8),
        ("Heading 3", 11.5, 10, 6),
    ):
        st = doc.styles[name]
        st.font.name = "Times New Roman"
        st.font.size = Pt(size)
        st.font.bold = True
        st.font.color.rgb = INK
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.line_spacing = 1.3
        st.paragraph_format.keep_with_next = True

    # 首页（封面）不显示页眉页脚
    sec.different_first_page_header_footer = True

    # 页眉页脚
    header = sec.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = header.add_run("认知副驾 · 分析设计文档（对象模型）")
    set_run_font(run, size=9, color=MUTED)
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_page_field(footer)
    return doc


def cover(doc: Document, title: str, subtitle: str, meta: list[list[str]]):
    for _ in range(3):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(title)
    set_run_font(run, size=20, bold=True, color=INK, east_font="黑体")

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(subtitle)
    set_run_font(run, size=11, color=MUTED)

    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p_border(p, edge="bottom", size=8, color=RULE, space=1)

    for _ in range(2):
        doc.add_paragraph()

    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    cell_margins(table, left=120, right=120, top=60, bottom=60)
    layout = OxmlElement("w:tblLayout")
    layout.set(qn("w:type"), "fixed")
    table._tbl.tblPr.append(layout)
    for key, value in meta:
        row = table.add_row()
        c0, c1 = row.cells
        c0.width = Cm(3.6)
        c1.width = Cm(11.0)
        shade_cell(c0, HEAD_FILL)
        p0 = c0.paragraphs[0]
        p0.paragraph_format.line_spacing = 1.2
        p0.paragraph_format.space_after = Pt(0)
        add_inline(p0, key, size=9.5, bold=True)
        p1 = c1.paragraphs[0]
        p1.paragraph_format.line_spacing = 1.2
        p1.paragraph_format.space_after = Pt(0)
        add_inline(p1, value, size=9.5)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


# ---------- Markdown 解析 ----------
def render_table(doc: Document, rows: list[list[str]]):
    width = len(rows[0])
    table = doc.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    cell_margins(table)
    tblpr = table._tbl.tblPr
    tblw = OxmlElement("w:tblW")
    tblw.set(qn("w:w"), "5000")
    tblw.set(qn("w:type"), "pct")
    tblpr.append(tblw)

    for i, cells in enumerate(rows):
        row = table.add_row()
        trpr = row._tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:cantSplit"))
        cells = (cells + [""] * width)[:width]
        for j, text in enumerate(cells):
            cell = row.cells[j]
            for k, line in enumerate(str(text).replace("\\n", "\n").split("\n")):
                para = cell.paragraphs[0] if k == 0 else cell.add_paragraph()
                para.paragraph_format.line_spacing = 1.15
                para.paragraph_format.space_after = Pt(0)
                para.paragraph_format.space_before = Pt(0)
                para.paragraph_format.keep_together = True
                if i == 0:
                    shade_cell(cell, HEAD_FILL)
                    para.paragraph_format.keep_with_next = True
                    add_inline(para, line, size=9, bold=True, mono_size=8.5)
                else:
                    add_inline(para, line, size=9, mono_size=8.5)
    if table.rows:
        repeat_header(table.rows[0])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def render_image(doc: Document, path: Path, alt: str):
    if not path.exists():
        p = doc.add_paragraph()
        run = p.add_run(f"[缺图：{alt}]")
        set_run_font(run, size=10, color=RGBColor(0xC0, 0x39, 0x2B), bold=True)
        return
    w_px, h_px = png_size(path)
    w_cm = min(MAX_W_CM, w_px / 300 * 2.54)      # 图统一按 300 dpi 渲染（diagrams/_style.puml）
    h_cm = w_cm * h_px / w_px
    if h_cm > MAX_H_CM:
        scale = MAX_H_CM / h_cm
        w_cm *= scale
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(4)
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(str(path), width=Cm(w_cm))


def render_caption(doc: Document, text: str):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(10)
    add_inline(p, text.strip("*"), size=9, color=MUTED)


def build(md_text: str):
    doc = setup_document()

    if "\n---\n" in md_text:
        head, body = md_text.split("\n---\n", 1)
    else:
        head, body = "", md_text

    lines = head.strip().split("\n")
    title = ""
    subtitle = ""
    meta: list[list[str]] = []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("# "):
            title = s[2:].strip()
        elif s.startswith("**") and s.endswith("**"):
            subtitle = s.strip("*").strip()
        elif s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            meta.append(cells)
    cover(doc, title or "分析设计文档", subtitle, meta)

    lines = body.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.rstrip()
        s = line.strip()

        if not s:
            i += 1
            continue

        # 代码块
        if s.startswith("```"):
            i += 1
            block = []
            while i < n and not lines[i].strip().startswith("```"):
                block.append(lines[i].rstrip())
                i += 1
            i += 1
            for code_line in block:
                p = doc.add_paragraph()
                p.paragraph_format.line_spacing = 1.15
                p.paragraph_format.space_after = Pt(0)
                p.paragraph_format.left_indent = Cm(0.4)
                shade_paragraph(p, CODE_FILL)
                run = p.add_run(code_line if code_line else " ")
                set_run_font(run, size=9, mono=True, color=INK)
            doc.add_paragraph().paragraph_format.space_after = Pt(2)
            continue

        # 图片
        m = re.match(r"!\[(.*?)\]\((.*?)\)", s)
        if m:
            alt, rel = m.group(1), m.group(2)
            render_image(doc, (BASE / rel), alt)
            i += 1
            continue

        # 图注（独占一行的斜体）
        if s.startswith("*") and s.endswith("*") and not s.startswith("**") and len(s) > 2:
            render_caption(doc, s)
            i += 1
            continue

        # 分隔线
        if re.fullmatch(r"-{3,}", s):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p_border(p, edge="bottom", size=6, color=RULE, space=1)
            i += 1
            continue

        # 标题
        m = re.match(r"^(#{1,4})\s+(.*)$", s)
        if m:
            level = max(1, min(len(m.group(1)) - 1, 3))
            doc.add_heading("", level=level)
            para = doc.paragraphs[-1]
            add_inline(para, m.group(2), size={1: 15.5, 2: 13, 3: 11.5}[level],
                       color=INK, bold=True, east_font="黑体")
            i += 1
            continue

        # 表格
        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            render_table(doc, rows)
            continue

        # 引用
        if s.startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(6)
            p.paragraph_format.line_spacing = 1.4
            p_border(p, edge="left", size=12, color=RULE, space=8)
            add_inline(p, s.lstrip("> ").strip(), size=10, color=MUTED)
            i += 1
            continue

        # 列表
        m = re.match(r"^(\s*)[-*]\s+(.*)$", raw)
        if m:
            depth = len(m.group(1)) // 2
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6 + 0.6 * depth)
            p.paragraph_format.first_line_indent = Cm(-0.35)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.4
            run = p.add_run("· ")
            set_run_font(run, size=10.5, color=MUTED)
            add_inline(p, m.group(2))
            i += 1
            continue

        m = re.match(r"^(\s*)(\d+)\.\s+(.*)$", raw)
        if m:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.first_line_indent = Cm(-0.45)
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.line_spacing = 1.4
            run = p.add_run(f"{m.group(2)}. ")
            set_run_font(run, size=10.5, color=INK, bold=True)
            add_inline(p, m.group(3))
            i += 1
            continue

        # 普通段落
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        add_inline(p, s)
        i += 1

    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    text = SRC.read_text(encoding="utf-8")
    path = build(text)
    print(f"written: {path}")
