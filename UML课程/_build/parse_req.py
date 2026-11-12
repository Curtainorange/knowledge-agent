"""把 Word 导出的 HTML 解析成中间块序列（保真：标题/段落/列表/表格/图片/粗体）。

Word filtered HTML 的特点：
- 编码 gbk（meta 声明 gb2312）
- 标题是 <h1..h3>，列表是 <p class=MsoListNumber*>，编号数字写在正文里（"1."）
- 图片是外部文件 `<img src="reqdoc.files/imageNNN.png" width=528>`
- 表格无合并单元格（本文件已验证 rowspan=colspan=0）
"""
from __future__ import annotations

import html
import re
from pathlib import Path

TAG = re.compile(r"<[^>]+>")
# 只保留这些命名实体，其余交给 html.unescape


def clean_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    return s.strip()


def cell_text(td_html: str) -> str:
    parts = re.findall(r"<p[^>]*>(.*?)</p>", td_html, re.S)
    if not parts:
        parts = [td_html]
    texts = [clean_text(p) for p in parts]
    return "\n".join(t for t in texts if t)


def parse_table(tab_html: str) -> dict:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tab_html, re.S):
        cells = []
        for td in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S):
            cells.append({
                "text": cell_text(td),
                "bold": bool(re.search(r"<b>|<strong>", td)),
            })
        if cells:
            rows.append(cells)
    return {"kind": "table", "rows": rows}


def parse(html_path: Path) -> list[dict]:
    raw = html_path.read_bytes().decode("gbk", errors="replace")
    body = raw[raw.find("<body"):]
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)

    # 1. 图片先换成占位符，保持位置
    images: list[dict] = []

    def img_repl(m):
        width = re.search(r'width\s*=\s*"?(\d+)', m.group(0))
        images.append({
            "width_px": int(width.group(1)) if width else 528,
            "src": re.search(r'src\s*=\s*"?([^" >]+)', m.group(0)).group(1),
        })
        return f"\x00IMG{len(images) - 1}\x00"

    body = re.sub(r"<img[^>]*>", img_repl, body)

    # 2. 按 table 切分，保序
    chunks = re.split(r"(<table.*?</table>)", body, flags=re.S | re.I)

    blocks: list[dict] = []
    for chunk in chunks:
        if chunk.lower().startswith("<table"):
            blocks.append(parse_table(chunk))
            continue
        # 3. 段落 / 标题
        for m in re.finditer(
            r"<h([1-6])[^>]*>(.*?)</h\1>|<p[^>]*>(.*?)</p>", chunk, flags=re.S | re.I
        ):
            if m.group(1):
                blocks.append({
                    "kind": "h",
                    "level": int(m.group(1)),
                    "text": clean_text(m.group(2)),
                })
                continue
            ptag = m.group(0)
            text = m.group(3) or ""
            # 段内图片：拆成 文本段 + 图 + 文本段
            pieces = re.split(r"(\x00IMG\d+\x00)", text, flags=re.S)
            for piece in pieces:
                pm = re.match(r"\x00IMG(\d+)\x00", piece)
                if pm:
                    meta = images[int(pm.group(1))]
                    blocks.append({"kind": "img", **meta})
                    continue
                t = clean_text(piece)
                if not t:
                    continue
                is_list = "MsoList" in ptag
                is_num = bool(re.match(r"^\d+[.、]", t))
                blocks.append({
                    "kind": "li" if is_list else "p",
                    "text": t,
                    "ordered": is_num,
                    "bold": bool(re.search(r"<b>|<strong>", piece)),
                })
    return blocks


if __name__ == "__main__":
    import json
    from collections import Counter

    p = Path(__file__).with_name("reqdoc.html")
    bl = parse(p)
    print("blocks", len(bl), Counter(b["kind"] for b in bl).most_common())
    print("\n--- 前 30 块 ---")
    for b in bl[:30]:
        if b["kind"] in ("h", "p", "li"):
            print(f'[{b["kind"]}{b.get("level","")}] {b["text"][:72]}')
        elif b["kind"] == "img":
            print(f'[img] {b["src"]} w={b["width_px"]}')
        else:
            print(f'[table] {len(b["rows"])}x{len(b["rows"][0])} first="{b["rows"][0][0]["text"][:20]}"')
    Path(__file__).with_name("reqdoc_blocks.json").write_text(
        json.dumps(bl, ensure_ascii=False, indent=1), encoding="utf-8"
    )
