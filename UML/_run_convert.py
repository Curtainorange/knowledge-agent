# -*- coding: utf-8 -*-
"""Stage 3 转换：HTML → DOCX（html-to-docx skill 的 API 模式调用）。"""
import json
import os
import sys

SCRIPTS = (r"C:\Users\Curtain\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx"
           r"\5.5.6-wb.38337834.g5f969292.h4918b5ced607\skills\html-to-docx\scripts")
sys.path.insert(0, SCRIPTS)

from html_to_docx import convert, ConvertOptions  # noqa: E402

BASE = r"D:\知识学习智能体\output\1e41c810-e2ec-48d6-9cb0-c7563303897c"
INPUT = os.path.join(BASE, "stage2", "formatted-需求分析建模.html")
OUTPUT = os.path.join(BASE, "stage3", "需求分析建模_用例模型与对象模型_周宇航.docx")

html = open(INPUT, encoding="utf-8").read()
# 注意：convert(html, output_path=...) 会重建 ConvertOptions 并丢掉 base_dir，
# 因此这里把 output_path 一并写进 options，且不传 output_path 参数。
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

print(json.dumps(
    {
        "success": result.success,
        "docx_path": result.docx_path,
        "error": result.error,
        "warnings": result.warnings,
        "size": os.path.getsize(result.docx_path) if result.success and result.docx_path else 0,
    },
    ensure_ascii=False,
))
