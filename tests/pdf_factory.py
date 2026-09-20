"""最小 PDF 生成器（仅供测试）。

不引入 reportlab / fpdf 之类的依赖：手工拼出规范允许的 PDF 字节流
（对象 + xref + trailer），供 books 解析测试构造样本。

限制：只用内置 Type1 字体（Helvetica / WinAnsiEncoding），因此**只能写拉丁字符**。
中文 PDF 需要嵌入 CID 字体，代价远超测试收益；中文相关的处理逻辑
（伪空格压缩、软换行合并）改为直接对解析器的纯函数做单元测试。
"""
from __future__ import annotations

from typing import Iterable

_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792
_PAGE_LEFT = 72
_PAGE_TOP = 720
_LINE_HEIGHT = 16


def _escape(text: str) -> str:
    """转义 PDF 文本串里的三种特殊字符。"""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _page_stream(lines: Iterable[str]) -> str:
    parts = [f"BT /F1 12 Tf {_PAGE_LEFT} {_PAGE_TOP} Td {_LINE_HEIGHT} TL"]
    for line in lines:
        parts.append(f"({_escape(line)}) Tj T*")
    parts.append("ET")
    return "\n".join(parts)


def _assemble(objects: dict[int, bytes]) -> bytes:
    """把对象拼成完整 PDF：正文 + 交叉引用表 + trailer。"""
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objects[num] + b"\nendobj\n"

    start_xref = len(out)
    count = max(objects) + 1
    out += b"xref\n0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for num in range(1, count):
        out += b"%010d 00000 n \n" % offsets.get(num, 0)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\n" % count
    out += b"startxref\n%d\n%%%%EOF\n" % start_xref
    return bytes(out)


def build_pdf(
    pages: list[list[str]],
    *,
    title: str = "",
    author: str = "",
    outline: list[tuple[str, int]] | None = None,
) -> bytes:
    """构造一份 PDF。

    - `pages`：每页一个字符串列表（按行）
    - `title` / `author`：写入 Info 字典（用于验证元数据解析）
    - `outline`：[(书签标题, 起始页下标)]，构造扁平书签（用于验证按书签分章）
    """
    pages = [[str(line) for line in page] for page in pages]
    page_count = len(pages)
    outline = list(outline or [])

    try:
        for line in [ln for page in pages for ln in page] + [title, author]:
            line.encode("latin-1")
        for item_title, _ in outline:
            item_title.encode("latin-1")
    except UnicodeEncodeError as exc:  # pragma: no cover - 防止误用
        raise ValueError("测试 PDF 只能写拉丁字符（内置 Type1 字体所限）") from exc

    # 编号固定：1=Catalog 2=Pages 3=Info 4=Font，随后每页 (Contents, Page) 成对
    content_nums = [5 + 2 * i for i in range(page_count)]
    page_nums = [6 + 2 * i for i in range(page_count)]
    outlines_num = 5 + 2 * page_count
    item_nums = [outlines_num + 1 + i for i in range(len(outline))]

    objects: dict[int, bytes] = {}

    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if outline:
        catalog += f" /Outlines {outlines_num} 0 R"
    objects[1] = (catalog + " >>").encode("latin-1")

    kids = " ".join(f"{num} 0 R" for num in page_nums)
    objects[2] = (
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>"
    ).encode("latin-1")

    objects[3] = (
        f"<< /Title ({_escape(title)}) /Author ({_escape(author)}) >>"
    ).encode("latin-1")

    objects[4] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
        b"/Encoding /WinAnsiEncoding >>"
    )

    for i, lines in enumerate(pages):
        stream = _page_stream(lines).encode("latin-1")
        objects[content_nums[i]] = (
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
        objects[page_nums[i]] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 4 0 R >> >> "
            f"/Contents {content_nums[i]} 0 R >>"
        ).encode("latin-1")

    if outline:
        objects[outlines_num] = (
            f"<< /Type /Outlines /First {item_nums[0]} 0 R "
            f"/Last {item_nums[-1]} 0 R /Count {len(item_nums)} >>"
        ).encode("latin-1")
        for idx, (item_title, page_index) in enumerate(outline):
            page_index = max(0, min(page_count - 1, int(page_index)))
            item = (
                f"<< /Title ({_escape(item_title)}) "
                f"/Parent {outlines_num} 0 R "
                f"/Dest [{page_nums[page_index]} 0 R /Fit]"
            )
            if idx > 0:
                item += f" /Prev {item_nums[idx - 1]} 0 R"
            if idx + 1 < len(item_nums):
                item += f" /Next {item_nums[idx + 1]} 0 R"
            objects[item_nums[idx]] = (item + " >>").encode("latin-1")

    return _assemble(objects)
