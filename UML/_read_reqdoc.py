# -*- coding: utf-8 -*-
"""读取 docs/需求分析文档.docx 全文（段落 + 表格），导出 Markdown。只读，不修改源文件。

说明：本次为「读取既有文档内容」，MCP 的 doc_resolve_document_structure 在 compact/full
模式下 text_preview 上限 200 字符，不足以迁移全文；故按 tencent-local-office-edit 的降级路径
改用 python-docx 只读提取（不写回、不改动原文件）。
"""
import io
import os
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

SRC = r"D:\知识学习智能体\docs\需求分析文档.docx"
OUT = r"D:\知识学习智能体\output\_reqdoc_md\original.md"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
doc = Document(SRC)

out = []
stats = {"heading": 0, "para": 0, "table": 0, "image": 0}

for child in doc.element.body.iterchildren():
    if child.tag == qn("w:p"):
        p = Paragraph(child, doc)
        style = (p.style.name or "") if p.style is not None else ""
        text = " ".join(p.text.split())
        if not text:
            continue
        if style.lower().startswith("heading"):
            tail = style.split()[-1]
            lvl = int(tail) if tail.isdigit() else 2
            lvl = min(max(lvl, 1), 6)
            out.append("")
            out.append("#" * lvl + " " + text)
            stats["heading"] += 1
        else:
            out.append(text)
            stats["para"] += 1
    elif child.tag == qn("w:tbl"):
        t = Table(child, doc)
        rows = []
        ncol = 0
        for i, r in enumerate(t.rows):
            cells = [" ".join(c.text.split()).replace("|", "\\|") for c in r.cells]
            ncol = max(ncol, len(cells))
            rows.append("| " + " | ".join(cells) + " |")
            if i == 0:
                rows.append("|" + "---|" * len(cells))
        out.append("")
        out.append("\n".join(rows))
        stats["table"] += 1

# 图片统计（只读）
for blip in doc.element.body.iter(qn("a:blip")):
    stats["image"] += 1

io.open(OUT, "w", encoding="utf-8").write("\n".join(out) + "\n")
print(stats)
print("bytes:", os.path.getsize(OUT))
