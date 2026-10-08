# -*- coding: utf-8 -*-
"""Stage 2 驱动：用既有 md2typeset 实现为「需求分析文档（规范版）」产出 HTML。"""
import importlib
import os
import sys

sys.path.insert(0, r"D:\知识学习智能体\UML")
M = importlib.import_module("_md2typeset_html")

RID = "349740f3-fb8b-49f9-9170-6ff9e7afdab4"
BASE = os.path.join(r"D:\知识学习智能体\output", RID)

M.BASE = BASE
M.MD_PATH = os.path.join(BASE, "stage1", "final_draft.md")
M.OUT_HTML = os.path.join(BASE, "stage2", "formatted-需求分析文档.html")
M.OUT_TOKENS = os.path.join(BASE, "stage2", "design_tokens.json")
M.TITLE = "认知副驾 · 产品需求分析报告"
M.SUBTITLE = "个人知识学习 AI 智能体 —— 产品范围与需求基线"
M.CATEGORY = "产品需求分析 · 需求规格说明"
M.META = "周宇航　|　2026-10-08　|　V3.1"

M.build()
