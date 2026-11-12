"""把《需求分析文档（规范版）》与《分析设计文档》合并成一份规范说明书。

结构：封面 → 修订记录 → 目录 → 第一篇 需求分析（1–12 章）→ 第二篇 系统设计（13–17 章）→ 附录 A–D
规范动作：章号连续、节号跟随、图号按章、表号按章、术语统一、版式统一。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import docx_kit as K
from docx.enum.text import WD_BREAK
from docx.shared import Pt

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent                      # UML课程
OUT = ROOT / "认知副驾-需求分析与系统设计-V4.0.docx"

# ---------------- 第一篇：需求（HTML 解析结果） ----------------
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
          "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def load_req(path: Path) -> dict:
    blocks = json.loads(path.read_text(encoding="utf-8"))

    # 修订记录表单独取出
    rev = None
    for i, b in enumerate(blocks):
        if b["kind"] == "table" and b["rows"] and b["rows"][0][0]["text"].strip() == "版本":
            rev = b
            del blocks[i]
            break

    # 段内不应有硬换行；图片转绝对路径
    for b in blocks:
        if b["kind"] in ("p", "li", "h") and "text" in b:
            b["text"] = b["text"].replace("\r", "").replace("\n", " ").strip()
            b["text"] = re.sub(r"^(\d+[.、])\s+", r"\1 ", b["text"])
        if b["kind"] == "img":
            b["src"] = str((BASE / b["src"]).resolve())
            b["dpi"] = 96
            b["display_px"] = b.get("width_px")

    start = next(i for i, b in enumerate(blocks)
                 if b["kind"] == "h" and re.match(r"^一、", b["text"]))
    intro = [b for b in blocks[:start]
             if b["kind"] == "p" and b["text"].startswith("本报告定义")]
    body = blocks[start:]

    # 章号：一、项目背景 → h1 "1 项目背景"
    for b in body:
        if b["kind"] == "h" and b["level"] == 2:
            m = re.match(r"^([一二三四五六七八九十]+)、\s*(.*)$", b["text"])
            if m:
                b["level"] = 1
                b["text"] = f"{CN_NUM[m.group(1)]} {m.group(2)}"
            else:                                    # 附录 A / 附录 B
                b["level"] = 1
        elif b["kind"] == "h" and b["level"] == 3:
            b["level"] = 2
    return {"rev": rev, "intro": intro, "body": body}


# ---------------- 第二篇：设计（md 解析） ----------------
CH_MAP = {2: 13, 3: 14, 4: 15, 5: 16, 6: 17}
# 图号按「正文出现顺序」编，不按图的原始编号：D-2 在 4.2 出现，所以它是 15-1
FIG_MAP = {
    "A-1a": "14-1", "A-1b": "14-2", "A-1c": "14-3",
    "A-2a": "14-4", "A-2b": "14-5", "A-3": "14-6",
    "D-2": "15-1", "D-1a": "15-2", "D-1b": "15-3", "D-1c": "15-4",
    "D-3": "15-5", "D-4": "15-6",
}
MD_IMG = re.compile(r"!\[(.*?)\]\((.*?)\)")


def _w(text: str) -> int:
    """按显示宽度计字符数：中日韩字符算 2，其余算 1。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in str(text))


def auto_widths(rows: list[dict], total: float = 15.5) -> list[float] | None:
    """列宽按内容长度分配，避免长列被挤成一行一个字。

    少于 4 列时不干预（等宽即可）；某列过短会被抬到下限，过长会被压到上限，
    剩余空间按比例回分给其余列。
    """
    if not rows:
        return None
    ncol = max(len(r) for r in rows)
    if ncol < 4:
        return None
    need = []
    for j in range(ncol):
        lens = [_w(r[j]["text"]) for r in rows if j < len(r)]
        avg = sum(lens) / max(1, len(lens))
        need.append(max(8.0, min(34.0, avg)))
    s = sum(need)
    widths = [total * x / s for x in need]
    # 太窄的列补到 1.7cm，补的部分从最宽的列扣
    for _ in range(ncol):
        short = [i for i, w in enumerate(widths) if w < 2.0]
        if not short:
            break
        deficit = sum(2.0 - widths[i] for i in short)
        for i in short:
            widths[i] = 2.0
        widest = max(range(ncol), key=lambda i: widths[i])
        widths[widest] -= deficit
    scale = total / sum(widths)
    return [round(w * scale, 2) for w in widths]


def parse_md(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    text = text.split("\n---\n", 1)[1] if "\n---\n" in text else text
    lines = text.split("\n")
    blocks: list[dict] = []
    i, n = 0, len(lines)

    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue

        if s.startswith("```"):
            i += 1
            code = []
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i].rstrip())
                i += 1
            i += 1
            blocks.append({"kind": "code", "lines": code})
            continue

        m = MD_IMG.match(s)
        if m:
            # 图统一按 300 dpi 渲染，见 diagrams/_style.puml
            blocks.append({"kind": "img", "src": str((ROOT / m.group(2)).resolve()), "dpi": 300})
            i += 1
            continue

        if s.startswith("*") and s.endswith("*") and not s.startswith("**") and len(s) > 2:
            blocks.append({"kind": "figcap", "text": s.strip("*").strip()})
            i += 1
            continue

        if re.fullmatch(r"-{3,}", s):
            i += 1
            continue

        m = re.match(r"^(#{2,4})\s+(.*)$", s)
        if m:
            blocks.append({"kind": "h", "level": max(1, len(m.group(1)) - 1), "text": m.group(2)})
            i += 1
            continue

        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append([{"text": c.replace("\\n", "\n"), "bold": False} for c in cells])
                i += 1
            blocks.append({"kind": "table", "rows": rows, "widths": auto_widths(rows)})
            continue

        if s.startswith(">"):
            blocks.append({"kind": "quote", "text": s.lstrip("> ").strip()})
            i += 1
            continue

        if re.match(r"^[-*]\s+", raw):
            body = re.sub(r"^\s*[-*]\s+", "", raw)
            depth = (len(raw) - len(raw.lstrip())) // 2
            blocks.append({"kind": "li", "text": ("　" * depth) + body})
            i += 1
            continue

        m = re.match(r"^(\d+)\.\s+(.*)$", s)
        if m:
            blocks.append({"kind": "li", "text": f"{m.group(1)}. {m.group(2)}"})
            i += 1
            continue

        blocks.append({"kind": "p", "text": s})
        i += 1
    return blocks


def redesign(blocks: list[dict]) -> list[dict]:
    """第二篇重编号：章 2–6 → 13–17；节号跟随；图号改名；附录改字母。"""
    # 1. 丢掉原第 1 章（文档说明，与第一篇重复）
    out, skipping = [], False
    for b in blocks:
        if b["kind"] == "h" and b["level"] == 1:
            skipping = b["text"].startswith("1. 文档说明") or b["text"].startswith("1 ")
            if skipping:
                continue
        if skipping:
            continue
        out.append(b)
    blocks = out

    # 2. 章 / 节号重排
    for b in blocks:
        if b["kind"] != "h":
            continue
        t = b["text"]
        if b["level"] == 1:
            m = re.match(r"^(\d+)\.\s*(.*)$", t)
            if m and int(m.group(1)) in CH_MAP:
                b["text"] = f"{CH_MAP[int(m.group(1))]} {m.group(2)}"
            elif t.startswith("附录 A"):
                b["text"] = "附录 C　图表清单与重渲染"
            elif t.startswith("附录 B"):
                b["text"] = "附录 D　编号索引"
        else:
            m = re.match(r"^(\d+)(\.\d+)+(.*)$", t)
            if m:
                head = int(m.group(1))
                if head in CH_MAP:
                    b["text"] = f"{CH_MAP[head]}{t[len(str(head)):]}"

    # 3. 图号与附录引用
    def fix(s: str) -> str:
        if not isinstance(s, str):
            return s
        for old in sorted(FIG_MAP, key=len, reverse=True):
            s = s.replace(old, FIG_MAP[old])
        s = s.replace("附录 A", "附录 C").replace("附录 B", "附录 D")
        s = s.replace("附录 C　图表清单", "附录 C　图表清单")
        return s

    sec_re = re.compile(r"(?<![\d.VRvr§])(\d)(\.\d+){1,2}(?![\d.])")

    def fix_sec(s: str) -> str:
        def remap(m):
            head = int(m.group(1))
            return f"{CH_MAP[head]}{m.group(0)[1:]}" if head in CH_MAP else m.group(0)
        # 合并后以第一篇（规范版）的章节为准，修正对旧章节号的引用
        s = s.replace("《需求分析文档》V3.0（UC-G/L1–L5，§6.1 数据实体）",
                      "《需求分析文档（规范版）》V3.1（§8.1 核心数据实体）")
        s = s.replace("第 5 章走查", "第 16 章走查")
        s = s.replace("§6.1", "§8.1")
        out = sec_re.sub(remap, s)
        return out.replace("14.6、6", "14.6、17")   # 附录 D 里「14.6、6」的第 6 章应为 17

    for b in blocks:
        if "text" in b:
            b["text"] = fix_sec(fix(b["text"]))
        if b["kind"] == "table":
            for row in b["rows"]:
                for cell in row:
                    cell["text"] = fix_sec(fix(cell["text"]))
    return blocks


# ---------------- 组装 ----------------
def add_tab_labels(blocks: list[dict]) -> list[dict]:
    """按章给表编号，插入 tablabel 块（放在表前）。"""
    out, chapter, seq = [], "0", 0
    for b in blocks:
        if b["kind"] == "h" and b["level"] == 1:
            m = re.match(r"^(\d+)", b["text"])
            chapter = m.group(1) if m else ("C" if b["text"].startswith("附录 C") else "D")
            seq = 0
        if b["kind"] == "table":
            seq += 1
            out.append({"kind": "tablabel", "text": f"表 {chapter}-{seq}"})
        out.append(b)
    return out


def add_req_fig_captions(blocks: list[dict]) -> list[dict]:
    """按章给图编号。原文已有图注（形如「图 3　…」）就改编号沿用，缺注的用节标题补。"""
    out, chapter, section, seq = [], "0", "", 0
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b["kind"] == "h" and b["level"] == 1:
            m = re.match(r"^(\d+)", b["text"])
            chapter = m.group(1) if m else "A"
            section, seq = "", 0
        elif b["kind"] == "h" and b["level"] == 2:
            section = re.sub(r"^\d+(\.\d+)+\s*", "", b["text"])
        if b["kind"] == "img":
            seq += 1
            out.append(b)
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt and nxt["kind"] == "p" and re.match(r"^图\s*\d+", nxt["text"]):
                text = re.sub(r"^图\s*\d+", f"图 {chapter}-{seq}", nxt["text"], count=1)
                out.append({"kind": "figcap", "text": text})
                i += 2
                continue
            out.append({"kind": "figcap", "text": f"图 {chapter}-{seq}　{section}"})
            i += 1
            continue
        out.append(b)
        i += 1
    return out


def build():
    req = load_req(BASE / "reqdoc_blocks.json")
    design = redesign(parse_md(ROOT / "分析设计文档.md"))

    doc = K.setup_document("认知副驾 · 需求分析与系统设计说明书 V4.0")
    K.cover(
        doc,
        "认知副驾 · 需求分析与系统设计说明书",
        "个人知识学习 AI 智能体 —— 需求基线、对象模型与系统设计",
        [
            ("文档编号", "COG-SDS-002"),
            ("版本 / 日期", "V4.0（修订 2）　/　2026-11-12"),
            ("编制", "周宇航"),
            ("合并说明", "由《产品需求分析报告》V3.1 与《分析设计文档》V1.2 合并重排：章号、图号、表号、术语统一"),
            ("适用范围", "需求评审、设计评审、开发实现、测试验收"),
        ],
    )

    # 修订记录
    p = doc.add_paragraph()
    run = p.add_run("文档修订记录")
    K.set_run_font(run, size=15.5, bold=True, color=K.INK, east_font=K.HEAD_EAST)
    p.paragraph_format.space_after = Pt(8)

    rev_rows = req["rev"]["rows"] if req["rev"] else [
        [{"text": "版本"}, {"text": "日期"}, {"text": "修订内容"}, {"text": "编制"}]
    ]
    rev_rows = [list(r) for r in rev_rows]
    rev_rows.append([
        {"text": "V4.0", "bold": False},
        {"text": "2026-11-12", "bold": False},
        {"text": "周宇航", "bold": False},
        {"text": "与《分析设计文档》V1.0 合并为需求+设计合订本：新增第二篇系统设计"
                 "（领域类图、BCE 分析类图、状态图、设计类图、包图、顺序图），"
                 "全文重排章号/图号/表号，统一版式与术语", "bold": False},
    ])
    rev_rows.append([
        {"text": "V4.0-r2", "bold": False},
        {"text": "2026-11-12", "bold": False},
        {"text": "周宇航", "bold": False},
        {"text": "补充四条核心功能规约（FS-01–04）与五条操作契约（OC-01–05），"
                 "新增「选定功能 L2 的设计类详解」一节；12 张图按统一风格重绘："
                 "正交直线走线、300 dpi、去图内标题，设计类图拆为接口/实体/端口三张，"
                 "新增反馈链顺序图（图 15-6）", "bold": False},
    ])
    K.render_table(doc, {"kind": "table", "rows": rev_rows, "widths": [1.7, 2.2, 1.8, 9.8]})
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    K.toc_page(doc)

    # 第一篇
    K.part_title(doc, "第一篇　需求分析", "需求基线 · 第 1–12 章")
    for b in req["intro"]:
        K.render_blocks(doc, add_tab_labels(add_req_fig_captions([b])))
    K.render_blocks(doc, add_tab_labels(add_req_fig_captions(req["body"])))

    # 第二篇
    K.part_title(doc, "第二篇　系统设计", "对象模型与系统设计 · 第 13–17 章")
    intro = {
        "kind": "p",
        "text": "本篇承接第一篇的需求基线，给出对象模型与系统设计：分析阶段识别领域概念与职责分工，"
                "设计阶段落到类、接口、可见性与依赖方向。本篇不重复第一篇的用例与规则原文，"
                "只在需要时以编号引用（UC-*、IN-01、OUT-01、BR-L2-01/02、AT-L2-01–04）。",
    }
    K.render_blocks(doc, add_tab_labels([intro] + design))

    doc.save(OUT)
    print(f"written: {OUT}")


if __name__ == "__main__":
    build()
