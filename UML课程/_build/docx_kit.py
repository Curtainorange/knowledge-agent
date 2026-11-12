"""通用 docx 渲染工具：封面、修订记录、目录、正文块、图表题注、页眉页脚。

被 build_merged.py 调用；也可单独用于其他文档。
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

INK = RGBColor(0x1F, 0x2D, 0x3D)
MUTED = RGBColor(0x5D, 0x6D, 0x7E)
RULE = "B3BECB"
HEAD_FILL = "F2F5F8"
CODE_FILL = "F7F8FA"
BODY_EAST = "宋体"
HEAD_EAST = "黑体"
MAX_W_CM = 15.5
MAX_H_CM = 16.5

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")


# ---------------- 基础 ----------------
def set_run_font(run, *, size=None, bold=None, color=None, mono=False,
                 italic=None, east_font=BODY_EAST):
    ascii_font = "Consolas" if mono else "Times New Roman"
    east = "Consolas" if mono else east_font
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


def add_field(paragraph, instr: str, *, size=9, color=MUTED):
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = instr
    sep = OxmlElement("w:fldChar")
    sep.set(qn("w:fldCharType"), "separate")
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for el in (begin, it, sep, end):
        run._r.append(el)
    set_run_font(run, size=size, color=color)


def image_size_cm(path: Path, dpi: int) -> tuple[float, float]:
    with path.open("rb") as fh:
        head = fh.read(33)
    w, h = struct.unpack(">II", head[16:24])
    w_cm = w / dpi * 2.54
    h_cm = h / dpi * 2.54
    return w_cm, h_cm


def fit_size(w_cm: float, h_cm: float) -> tuple[float, float]:
    if w_cm > MAX_W_CM:
        h_cm *= MAX_W_CM / w_cm
        w_cm = MAX_W_CM
    if h_cm > MAX_H_CM:
        w_cm *= MAX_H_CM / h_cm
        h_cm = MAX_H_CM
    return w_cm, h_cm


# ---------------- 文档骨架 ----------------
def setup_document(header_text: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Cm(21.0)
    sec.page_height = Cm(29.7)
    sec.left_margin = Cm(2.5)
    sec.right_margin = Cm(2.5)
    sec.top_margin = Cm(2.5)
    sec.bottom_margin = Cm(2.3)

    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_EAST)
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
        st.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_EAST)
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.line_spacing = 1.3
        st.paragraph_format.keep_with_next = True

    sec.different_first_page_header_footer = True
    hp = sec.header.paragraphs[0]
    hp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = hp.add_run(header_text)
    set_run_font(run, size=9, color=MUTED)
    fp = sec.footer.paragraphs[0]
    fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_field(fp, "PAGE")
    return doc


def cover(doc: Document, title: str, subtitle: str, meta: list[tuple[str, str]]):
    for _ in range(3):
        doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(8)
    run = p.add_run(title)
    set_run_font(run, size=20, bold=True, color=INK, east_font=HEAD_EAST)

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
        c0.width = Cm(3.4)
        c1.width = Cm(11.2)
        shade_cell(c0, HEAD_FILL)
        for cell, text, bold in ((c0, key, True), (c1, value, False)):
            para = cell.paragraphs[0]
            para.paragraph_format.line_spacing = 1.2
            para.paragraph_format.space_after = Pt(0)
            run = para.add_run(text)
            set_run_font(run, size=9.5, bold=bold, color=INK)

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def toc_page(doc: Document, heading: str = "目　录"):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(12)
    run = p.add_run(heading)
    set_run_font(run, size=16, bold=True, color=INK, east_font=HEAD_EAST)

    p = doc.add_paragraph()
    add_field(p, r'TOC \o "1-3" \h \z \u', size=10.5, color=INK)


def part_title(doc: Document, text: str, note: str = ""):
    """篇扉页：前后都分页，独占一页，居中大字 + 一行说明。"""
    p = doc.add_paragraph()
    p.paragraph_format.page_break_before = True
    p.paragraph_format.space_before = Pt(150)
    p.paragraph_format.space_after = Pt(18)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p_border(p, edge="bottom", size=10, color=RULE, space=12)
    run = p.add_run(text)
    set_run_font(run, size=22, bold=True, color=INK, east_font=HEAD_EAST)
    if note:
        pn = doc.add_paragraph()
        pn.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pn.paragraph_format.space_before = Pt(10)
        rn = pn.add_run(note)
        set_run_font(rn, size=10.5, color=MUTED)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


# ---------------- 正文块 ----------------
def rich(paragraph, text: str, *, size=10.5, color=INK, bold=False):
    """渲染含 **粗体** 与 `等宽` 的行内文本。"""
    for piece in INLINE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            run = paragraph.add_run(piece[2:-2])
            set_run_font(run, size=size, bold=True, color=color)
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            run = paragraph.add_run(piece[1:-1])
            set_run_font(run, size=size - 0.5, color=color, mono=True)
        else:
            run = paragraph.add_run(piece)
            set_run_font(run, size=size, color=color, bold=bold)


def render_heading(doc: Document, block: dict):
    level = max(1, min(block["level"], 3))
    doc.add_heading("", level=level)
    para = doc.paragraphs[-1]
    rich(para, block["text"], size={1: 15.5, 2: 13, 3: 11.5}[level], bold=True)


def render_para(doc: Document, block: dict):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(6)
    rich(p, block["text"])


def render_list(doc: Document, block: dict):
    text = block["text"]
    m = re.match(r"^(\d+)[.、)）]\s*(.*)$", text)
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.75)
    p.paragraph_format.first_line_indent = Cm(-0.45)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.line_spacing = 1.4
    if m:
        run = p.add_run(f"{m.group(1)}. ")
        set_run_font(run, size=10.5, color=INK, bold=True)
        rich(p, m.group(2))
    else:
        bullet = "" if re.match(r"^[·•·\-—]", text) else "· "
        if not bullet:
            rich(p, text)
            return
        run = p.add_run(bullet)
        set_run_font(run, size=10.5, color=MUTED)
        rich(p, text)


def render_quote(doc: Document, block: dict):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.5)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing = 1.4
    p_border(p, edge="left", size=12, color=RULE, space=8)
    rich(p, block["text"], size=10, color=MUTED)


def render_code(doc: Document, block: dict):
    for line in block["lines"]:
        p = doc.add_paragraph()
        p.paragraph_format.line_spacing = 1.15
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Cm(0.4)
        shade_paragraph(p, CODE_FILL)
        run = p.add_run(line if line else " ")
        set_run_font(run, size=9, mono=True, color=INK)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def render_image(doc: Document, block: dict):
    path = Path(block["src"])
    if not path.exists():
        p = doc.add_paragraph()
        run = p.add_run(f"[缺图：{path.name}]")
        set_run_font(run, size=10, bold=True, color=RGBColor(0xC0, 0x39, 0x2B))
        return
    w_cm, h_cm = image_size_cm(path, block.get("dpi", 200))
    if block.get("display_px"):
        h_cm = block["display_px"] / 96 * 2.54 * (h_cm / w_cm)
        w_cm = block["display_px"] / 96 * 2.54
    w_cm, h_cm = fit_size(w_cm, h_cm)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(str(path), width=Cm(w_cm))


def render_caption(doc: Document, text: str):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_after = Pt(10)
    run = p.add_run(text)
    set_run_font(run, size=9, color=MUTED)


def render_table_label(doc: Document, text: str):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.keep_with_next = True
    run = p.add_run(text)
    set_run_font(run, size=9, color=MUTED)


def render_table(doc: Document, block: dict):
    rows = block["rows"]
    if not rows:
        return
    width = max(len(r) for r in rows)
    table = doc.add_table(rows=0, cols=width)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    widths = block.get("widths")
    table.autofit = not widths
    cell_margins(table)
    if widths:
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        table._tbl.tblPr.append(layout)
    tblpr = table._tbl.tblPr
    tblw = OxmlElement("w:tblW")
    tblw.set(qn("w:w"), "5000")
    tblw.set(qn("w:type"), "pct")
    tblpr.append(tblw)

    for i, cells in enumerate(rows):
        row = table.add_row()
        trpr = row._tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:cantSplit"))
        cells = (cells + [{"text": "", "bold": False}] * width)[:width]
        is_head = i == 0 or all(c.get("bold") for c in cells)
        for j, cell_data in enumerate(cells):
            cell = row.cells[j]
            if widths:
                cell.width = Cm(widths[j])
            para = cell.paragraphs[0]
            para.paragraph_format.line_spacing = 1.15
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.space_before = Pt(0)
            para.paragraph_format.keep_together = True
            if is_head:
                shade_cell(cell, HEAD_FILL)
                para.paragraph_format.keep_with_next = True
            for k, line in enumerate(str(cell_data.get("text", "")).split("\n")):
                target = para if k == 0 else cell.add_paragraph()
                if k:
                    target.paragraph_format.line_spacing = 1.15
                    target.paragraph_format.space_after = Pt(0)
                rich(target, line, size=9, bold=bool(cell_data.get("bold")) or is_head)
    if table.rows:
        repeat_header(table.rows[0])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def render_blocks(doc: Document, blocks: list[dict]):
    for b in blocks:
        kind = b["kind"]
        if kind == "h":
            render_heading(doc, b)
        elif kind == "p":
            render_para(doc, b)
        elif kind == "li":
            render_list(doc, b)
        elif kind == "quote":
            render_quote(doc, b)
        elif kind == "code":
            render_code(doc, b)
        elif kind == "img":
            render_image(doc, b)
        elif kind == "figcap":
            render_caption(doc, b["text"])
        elif kind == "tablabel":
            render_table_label(doc, b["text"])
        elif kind == "table":
            render_table(doc, b)
        elif kind == "part":
            part_title(doc, b["text"])
        elif kind == "pagebreak":
            doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
