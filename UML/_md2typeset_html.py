# -*- coding: utf-8 -*-
"""把 stage1/final_draft.md 转为 doc-typeset 规范的 HTML（business-report 主题）。

规范落实点（doc-typeset SKILL.md / prompts/base.md）：
  - :root 注入 design token CSS 变量 + 本地别名映射；样式一律 var()，无裸值
  - 封面 section[role=cover] 与正文 section[role=body] 为唯一顶层 section，封面每块显式 text-align
  - 表格一律 <thead>+<tbody>；图片 <figure>+<figcaption>；h2≥3 自动目录
  - 引用块用段落级 border-left + 前导 &nbsp;&nbsp;；无装饰短横线；无 grid/dl/span-badge
"""
import re
import json
import html
import os

BASE = r"D:\知识学习智能体\output\1e41c810-e2ec-48d6-9cb0-c7563303897c"
MD_PATH = os.path.join(BASE, "stage1", "final_draft.md")
TOKEN_PATH = (r"C:\Users\Curtain\.workbuddy\plugins\cache\workbuddy-builtin\tencent-docx"
              r"\5.5.6-wb.38337834.g5f969292.h4918b5ced607\skills\design-token\tokens"
              r"\compiled\business-report.json")
OUT_HTML = os.path.join(BASE, "stage2", "formatted-需求分析建模.html")
OUT_TOKENS = os.path.join(BASE, "stage2", "design_tokens.json")

TITLE = "需求分析建模：用例模型与部分对象模型"
SUBTITLE = "认知副驾（Cognitive Copilot）—— 个人知识学习 AI 智能体"
CATEGORY = "面向对象分析与设计 · 项目需求分析建模"
META = "组别 / 成员：【待填】　|　2026-10-08（星期四）　|　v1.0"


# ---------------- 行内转换 ----------------
def inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    return t


def esc(text: str) -> str:
    return html.escape(text, quote=False)


# ---------------- 表格 ----------------
def split_row(row: str):
    return [c.strip() for c in row.strip().strip("|").split("|")]


def render_table(rows):
    head = split_row(rows[0])
    body = [split_row(r) for r in rows[2:]]
    out = ['<table>']
    out.append("<thead><tr>" + "".join("<th>%s</th>" % inline(c) for c in head) + "</tr></thead>")
    out.append("<tbody>")
    for r in body:
        out.append("<tr>" + "".join("<td>%s</td>" % inline(c) for c in r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


# ---------------- 主解析 ----------------
def convert(md: str):
    lines = md.split("\n")
    blocks = []          # [(kind, payload)]
    toc = []
    i = 0
    h2n = 0
    pending_caption_img = None

    while i < len(lines):
        line = lines[i]
        s = line.strip()

        if not s:
            i += 1
            continue

        # H1：文档标题（封面用）
        if s.startswith("# ") and not s.startswith("## "):
            i += 1
            continue

        # H2
        if s.startswith("## "):
            h2n += 1
            txt = s[3:].strip()
            anchor = "section-%d" % h2n
            toc.append((anchor, txt))
            blocks.append(("h2", (anchor, txt)))
            i += 1
            continue

        # H3 / H4
        if s.startswith("### "):
            blocks.append(("h3", s[4:].strip()))
            i += 1
            continue
        if s.startswith("#### "):
            blocks.append(("h4", s[5:].strip()))
            i += 1
            continue

        # 分隔线（md 的 --- 只作分节意图，不产装饰线）
        if re.fullmatch(r"-{3,}", s):
            blocks.append(("divider", None))
            i += 1
            continue

        # 图片
        m = re.match(r"!\[(.*?)\]\((.*?)\)", s)
        if m:
            alt, src = m.group(1), m.group(2)
            # 紧随的「图N …」段落作为 figcaption（允许中间空行）
            cap = ""
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and re.match(r"^(图|表)\s*\d+", lines[j].strip()):
                cap = lines[j].strip()
                i = j
            blocks.append(("figure", (src, alt, cap)))
            i += 1
            continue

        # 表格
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:\-|]+\|$", lines[i + 1].strip()):
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            blocks.append(("table", rows))
            continue

        # 引用块
        if s.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(x for x in buf if x)))
            continue

        # 有序列表
        if re.match(r"^\d+\.\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\d+\.\s+", lines[i].strip()):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i].strip()))
                i += 1
            blocks.append(("ol", items))
            continue

        # 无序列表
        if s.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            blocks.append(("ul", items))
            continue

        # 普通段落（合并连续行）
        buf = [s]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or nxt.startswith("#") or nxt.startswith("|") or nxt.startswith("> ")
                    or nxt.startswith("- ") or re.match(r"^\d+\.\s+", nxt)
                    or nxt.startswith("![") or re.fullmatch(r"-{3,}", nxt)):
                break
            buf.append(nxt)
            i += 1
        blocks.append(("p", " ".join(buf)))

    return blocks, toc


def render(blocks, toc) -> str:
    out = []
    for kind, payload in blocks:
        if kind == "h2":
            anchor, txt = payload
            out.append('<h2 id="%s">%s</h2>' % (anchor, inline(txt)))
        elif kind == "h3":
            out.append("<h3>%s</h3>" % inline(payload))
        elif kind == "h4":
            out.append("<h4>%s</h4>" % inline(payload))
        elif kind == "p":
            out.append("<p>%s</p>" % inline(payload))
        elif kind == "quote":
            out.append('<p class="note-inline">&nbsp;&nbsp;%s</p>' % inline(payload))
        elif kind == "divider":
            out.append('<div class="section-divider"></div>')
        elif kind == "figure":
            src, alt, cap = payload
            fig = ['<figure><img src="%s" alt="%s">' % (esc(src), esc(alt))]
            if cap:
                fig.append("<figcaption>%s</figcaption>" % inline(cap))
            fig.append("</figure>")
            out.append("".join(fig))
        elif kind == "table":
            out.append(render_table(payload))
        elif kind == "ol":
            out.append("<ol>" + "".join("<li>%s</li>" % inline(x) for x in payload) + "</ol>")
        elif kind == "ul":
            out.append("<ul>" + "".join("<li>%s</li>" % inline(x) for x in payload) + "</ul>")
    return "\n".join(out)


# ---------------- 组装 HTML ----------------
def build():
    md = open(MD_PATH, encoding="utf-8").read()
    tokens = json.load(open(TOKEN_PATH, encoding="utf-8"))
    json.dump(tokens, open(OUT_TOKENS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    blocks, toc = convert(md)
    body_html = render(blocks, toc)
    toc_html = "".join('<li><a href="#%s">%s</a></li>' % (a, esc(t)) for a, t in toc)

    css_vars = "\n".join("    %s: %s;" % (k, v) for k, v in tokens["css_variables"].items())

    html_doc = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="docx-page-size" content="A4">
  <title>%(title)s</title>
  <style>
  /* --- 页面模型：封面无页脚，正文页脚居中页码 --- */
  @page {
    @bottom-center { content: counter(page); }
  }
  @page cover {
    @bottom-center { content: none; }
  }
  section[role="cover"] { page: cover; }

  :root {
    /* ==== Design Tokens（business-modern · business-report）==== */
%(tokens)s
    /* ==== 本地别名映射 ==== */
    --fs-title: var(--typography-fontSize-coverTitle, 22pt);
    --fs-h1: var(--typography-fontSize-h1, 20pt);
    --fs-h2: var(--typography-fontSize-h2, 11pt);
    --fs-h3: var(--typography-fontSize-h3, 10pt);
    --fs-h4: var(--typography-fontSize-h3, 10pt);
    --fs-body: var(--typography-fontSize-body, 9pt);
    --fs-small: var(--typography-fontSize-small, 8pt);
    --fs-tiny: var(--typography-fontSize-tiny, 7pt);
    --fs-caption: var(--typography-fontSize-caption, 9pt);
    --ff-heading: var(--typography-fontFamily-heading);
    --ff-body: var(--typography-fontFamily-body);
    --ff-mono: var(--typography-fontFamily-code, monospace);
    --lh-body: var(--typography-lineHeight-body, 1.6);
    --lh-heading: var(--typography-lineHeight-title, 1.3);
    --fw-bold: var(--typography-fontWeight-heading, 600);
    --fw-body: var(--typography-fontWeight-body, 400);
    --c-primary: var(--color-primary);
    --c-secondary: var(--color-secondary);
    --c-accent: var(--color-accent);
    --c-text: var(--color-text);
    --c-muted: var(--color-textSecondary);
    --c-heading: var(--color-heading);
    --c-divider: var(--color-divider);
    --c-bg: var(--color-background);
    --c-surface: var(--color-surface);
    --sp-para: var(--spacing-paragraph, 0.75em);
    --sp-section: var(--spacing-sectionGap, 2.5em);
    --sp-block: var(--spacing-paragraphAfter, 0.75em);
    --page-content-width: var(--layout-maxContentWidth, 15.6cm);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: var(--ff-body);
    font-size: var(--fs-body);
    line-height: var(--lh-body);
    color: var(--c-text);
    background: var(--c-bg);
    max-width: var(--page-content-width);
    margin-left: auto;
    margin-right: auto;
  }

  /* --- 封面：每个块级元素各自显式声明 text-align --- */
  .cover-category {
    font-family: var(--ff-heading);
    font-size: var(--typography-fontSize-coverCategory, 14pt);
    font-weight: 700;
    color: var(--c-primary);
    letter-spacing: var(--tracking-category, 0.08em);
    text-align: center;
    margin-bottom: var(--sp-block);
  }
  .cover-title {
    font-family: var(--ff-heading);
    font-size: var(--fs-title);
    font-weight: 700;
    color: var(--c-heading);
    line-height: var(--lh-heading);
    text-align: center;
    margin-bottom: var(--sp-block);
  }
  .cover-subtitle {
    font-family: var(--ff-body);
    font-size: var(--typography-fontSize-subtitle, 18pt);
    color: var(--c-muted);
    text-align: center;
    margin-bottom: var(--sp-section);
  }
  .cover-meta {
    font-family: var(--ff-body);
    font-size: var(--fs-small);
    color: var(--c-muted);
    text-align: center;
    margin-bottom: var(--sp-block);
  }

  /* --- 标题层级：每个块自己声明字号/字族/颜色 --- */
  h2 {
    font-family: var(--ff-heading);
    font-size: var(--fs-h2);
    font-weight: 700;
    color: var(--c-heading);
    line-height: var(--lh-heading);
    text-align: left;
    margin-top: var(--sp-section);
    margin-bottom: var(--sp-para);
    border-left: var(--bd-heading, 4px) solid var(--c-primary);
    padding-left: var(--sp-block);
  }
  h3 {
    font-family: var(--ff-heading);
    font-size: var(--fs-h3);
    font-weight: 700;
    color: var(--c-primary);
    text-align: left;
    margin-top: var(--sp-block);
    margin-bottom: var(--sp-para);
  }
  h4 {
    font-family: var(--ff-heading);
    font-size: var(--fs-h4);
    font-weight: 700;
    color: var(--c-text);
    text-align: left;
    margin-top: var(--sp-block);
    margin-bottom: var(--sp-para);
  }

  p {
    font-family: var(--ff-body);
    font-size: var(--fs-body);
    color: var(--c-text);
    text-align: left;
    margin-bottom: var(--sp-para);
  }
  ol, ul {
    font-family: var(--ff-body);
    font-size: var(--fs-body);
    color: var(--c-text);
    margin-bottom: var(--sp-para);
    padding-left: var(--sp-section);
  }
  li { margin-bottom: var(--sp-block); }
  strong { font-weight: 700; color: var(--c-text); }
  code {
    font-family: var(--ff-mono);
    font-size: var(--fs-small);
    color: var(--c-text);
  }

  table {
    width: 100%%;
    border-collapse: collapse;
    margin-bottom: var(--sp-block);
    font-family: var(--ff-body);
    font-size: var(--fs-small);
  }
  th, td {
    border: 1px solid var(--c-divider);
    padding: var(--sp-cell-y, 0.35em) var(--sp-cell-x, 0.5em);
    text-align: left;
    vertical-align: top;
    font-size: var(--fs-small);
  }
  th {
    font-family: var(--ff-heading);
    font-size: var(--fs-small);
    font-weight: 700;
    color: var(--c-heading);
    background: var(--c-surface);
  }

  figure { margin: var(--sp-block) var(--sp-zero, 0) var(--sp-section) var(--sp-zero, 0); text-align: center; }
  figure img { max-width: 100%%; }
  figcaption {
    font-family: var(--ff-body);
    font-size: var(--fs-caption);
    color: var(--c-muted);
    text-align: center;
    margin-top: var(--sp-block);
  }

  /* --- 目录 --- */
  .doc-toc {
    background: var(--c-surface);
    padding: var(--sp-block) var(--sp-section);
    margin-bottom: var(--sp-section);
  }
  .toc-title {
    font-family: var(--ff-heading);
    font-size: var(--fs-h3);
    font-weight: 700;
    color: var(--c-heading);
    text-align: left;
    margin-bottom: var(--sp-para);
  }
  .toc-list, .toc-list ol, .toc-list ul {
    list-style: none;
    list-style-type: none;
    padding-left: var(--sp-block);
  }
  .toc-list li { font-family: var(--ff-body); font-size: var(--fs-body); margin-bottom: var(--sp-toc-item, 0.2em); }
  .toc-list a { color: var(--c-primary); text-decoration: none; }

  /* --- 单段落强调（引用块）：左边框前导空格见 base.md §e --- */
  .note-inline {
    border-left: var(--bd-note, 3px) solid var(--c-primary);
    background: var(--c-surface);
    padding: var(--sp-note-y, 0.5em) var(--sp-note-x, 0.75em);
  }

  .section-divider {
    border-top: var(--bd-divider, 1px) solid var(--c-divider);
    margin: var(--sp-section) 0;
  }
  </style>
</head>
<body>
  <section role="cover">
    <p class="cover-category">%(category)s</p>
    <h1 class="cover-title">%(title)s</h1>
    <p class="cover-subtitle">%(subtitle)s</p>
    <p class="cover-meta">%(meta)s</p>
  </section>

  <section role="body" data-page-restart="1">
    <nav class="doc-toc" aria-label="文档目录">
      <p class="toc-title">目录</p>
      <ol class="toc-list">%(toc)s</ol>
    </nav>

    <main>
%(body)s
    </main>
  </section>
</body>
</html>
""" % {
        "title": esc(TITLE),
        "tokens": css_vars,
        "category": esc(CATEGORY),
        "subtitle": esc(SUBTITLE),
        "meta": esc(META),
        "toc": toc_html,
        "body": body_html,
    }

    open(OUT_HTML, "w", encoding="utf-8").write(html_doc)
    print("written:", OUT_HTML, os.path.getsize(OUT_HTML), "bytes")
    print("toc entries:", len(toc))
    print("blocks:", len(blocks))


if __name__ == "__main__":
    build()
