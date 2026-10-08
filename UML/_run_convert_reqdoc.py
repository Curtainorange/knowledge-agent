# -*- coding: utf-8 -*-
"""Stage 3：把需求分析文档（规范版）的 HTML 转为 DOCX。"""
import json
import os
import sys

SCRIPTS = (r"C:\Users\Curtain\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx"
           r"\5.5.6-wb.38337834.g5f969292.h4918b5ced607\skills\html-to-docx\scripts")
sys.path.insert(0, SCRIPTS)

from html_to_docx import convert, ConvertOptions  # noqa: E402

RID = "349740f3-fb8b-49f9-9170-6ff9e7afdab4"
BASE = os.path.join(r"D:\知识学习智能体\output", RID)
INPUT = os.path.join(BASE, "stage2", "formatted-需求分析文档.html")
OUTPUT = r"D:\知识学习智能体\docs\需求分析文档（规范版）.docx"

html = open(INPUT, encoding="utf-8").read()
# base_dir 必须走 options（convert 传 output_path 会重建 options 并丢掉 base_dir）
options = ConvertOptions(
    page_size="A4",
    orientation="portrait",
    margin_top=2.5,
    margin_bottom=2.0,
    margin_left=2.5,
    margin_right=2.5,
    output_path=OUTPUT,
    base_dir=os.path.dirname(INPUT),
)
result = convert(html, options=options)

info = {
    "success": result.success,
    "docx_path": result.docx_path,
    "error": result.error,
    "warnings": result.warnings,
    "size": os.path.getsize(result.docx_path) if result.success and result.docx_path else 0,
}
print(json.dumps(info, ensure_ascii=False))

if result.success:
    import zipfile
    z = zipfile.ZipFile(result.docx_path)
    doc = z.read("word/document.xml").decode("utf-8")
    print("media:", len([n for n in z.namelist() if n.startswith("word/media")]))
    print("drawings:", doc.count("<w:drawing>"), "tables:", doc.count("<w:tbl>"),
          "sections:", doc.count("<w:sectPr"))
