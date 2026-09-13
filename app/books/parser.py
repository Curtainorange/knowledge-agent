"""书籍解析：.txt / .epub → (标题, 作者, 章节结构, 全文)。

- txt：探测编码（utf-8 → gb18030 兜底），按章节标题正则切分。
- epub：标准 OCF 容器（本质是 zip）→ 读 container.xml 定位 OPF → 按 spine 顺序
  把每个 xhtml 文档当作一章，用 BeautifulSoup 抽取标题与纯文本。
  全程零第三方 epub 依赖（只用标准库 zipfile/xml + 已装依赖 bs4）。
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

# 常见章节标题：中文章回体 / 英文 Chapter / 序跋附录
_CHAPTER_PATTERNS = [
    re.compile(r"^[ \t]*(第[0-9一二三四五六七八九十百千万零两]+[章节回卷篇部][^\n]{0,60})", re.M),
    re.compile(r"^[ \t]*(Chapter|CHAPTER)\s+[0-9IVXLC]+[^\n]{0,60}", re.M),
    re.compile(r"^[ \t]*(序章|楔子|前言|引子|后记|尾声|附录|番外)[^\n]{0,60}", re.M),
]

_CHAPTER_TITLE_MAX = 80

# 图片扩展名 → MIME（epub 内插图常见格式）
_IMG_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".bmp": "image/bmp",
}

# 全文里图片的占位标记：阅读器据此把图片插回正文对应位置
_IMG_PLACEHOLDER = "[[IMG:{name}]]"


@dataclass
class ParsedBook:
    title: str
    author: str
    chapters: list[dict]  # [{index, title, char_start, char_end}]
    full_text: str
    images: list[dict] = field(default_factory=list)  # [{name, data, mime}]


def _read_with_encoding(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("utf-8", errors="replace")


def _split_chapters(text: str) -> list[dict]:
    """按章节标题切分；识别不到时整本作为单章。"""
    positions: list[tuple[int, str]] = []
    for pattern in _CHAPTER_PATTERNS:
        for match in pattern.finditer(text):
            positions.append((match.start(), match.group(1).strip()))

    # 去重（同一位置可能被多个模式命中），按位置排序
    unique: dict[int, str] = {}
    for pos, title in positions:
        if pos not in unique or len(title) > len(unique[pos]):
            unique[pos] = title
    ordered = sorted(unique.items())

    if not ordered:
        return [{"index": 1, "title": "全文", "char_start": 0, "char_end": len(text)}]

    chapters: list[dict] = []
    for i, (pos, title) in enumerate(ordered):
        end = ordered[i + 1][0] if i + 1 < len(ordered) else len(text)
        chapters.append(
            {"index": i + 1, "title": title[:_CHAPTER_TITLE_MAX], "char_start": pos, "char_end": end}
        )

    # 第一章标题前若还有正文（扉页 / 目录），补一个「开篇」
    if ordered[0][0] > 0:
        chapters.insert(
            0, {"index": 0, "title": "开篇", "char_start": 0, "char_end": ordered[0][0]}
        )
        for idx, chapter in enumerate(chapters, 1):
            chapter["index"] = idx
    return chapters


def parse_txt(path: Path, title: str | None = None) -> ParsedBook:
    raw = _read_with_encoding(path)
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    return ParsedBook(
        title=title or path.stem,
        author="",
        chapters=_split_chapters(text),
        full_text=text,
    )


def _strip_namespace(root: ET.Element) -> ET.Element:
    """去掉 XML 标签里的命名空间前缀，便于用简单 tag 名访问。"""
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _resolve_href(opf_dir: str, href: str) -> str:
    if opf_dir in ("", "."):
        return href
    joined = f"{opf_dir}/{href}".replace("//", "/")
    return joined


def _extract_images(soup, zf, *, xhtml_dir: str, opf_dir: str, images: list, counter: list) -> None:
    """把 soup 里的 <img> 替换为正文占位符，图片二进制收集进 images。

    img 的 src 可能相对「当前 xhtml 所在目录」或「OPF 目录」，两者都尝试读取；
    读不到（外链图 / 文件缺失）就丢弃该 img，不阻断正文解析。
    """
    for img in soup.find_all("img"):
        src = (img.get("src") or "").strip()
        href = src.split("#", 1)[0].split("?", 1)[0]
        if not href:
            img.decompose()
            continue
        data = None
        for base in (xhtml_dir, opf_dir):
            try:
                data = zf.read(_resolve_href(base, href))
                break
            except KeyError:
                continue
        if data is None:
            img.decompose()
            continue
        ext = Path(href).suffix.lower()
        if ext not in _IMG_MIME:
            ext = ".jpg"
        name = f"img_{counter[0]:04d}{ext}"
        counter[0] += 1
        images.append({"name": name, "data": data, "mime": _IMG_MIME.get(ext, "image/jpeg")})
        img.replace_with(_IMG_PLACEHOLDER.format(name=name))


def parse_epub(path: Path) -> ParsedBook:
    with zipfile.ZipFile(path) as zf:
        # 1) container.xml → OPF 路径
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = next(el for el in container.iter() if el.tag.endswith("rootfile"))
        opf_path = rootfile.get("full-path", "")
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")

        # 2) OPF → 元数据 / manifest / spine
        opf_root = _strip_namespace(ET.fromstring(zf.read(opf_path)))
        title = author = ""
        manifest: dict[str, str] = {}
        spine: list[str] = []
        for el in opf_root.iter():
            tag = el.tag
            if tag == "title" and not title:
                title = (el.text or "").strip()
            elif tag == "creator" and not author:
                author = (el.text or "").strip()
            elif tag == "item" and el.get("id"):
                manifest[el.get("id")] = el.get("href", "")
            elif tag == "itemref":
                spine.append(el.get("idref", ""))

        # 3) 按 spine 顺序抽取每个文档的标题与正文
        parts: list[str] = []
        headings: list[str] = []
        images: list[dict] = []
        img_counter = [0]
        for idref in spine:
            href = manifest.get(idref)
            if not href:
                continue
            full = _resolve_href(opf_dir, href)
            try:
                raw = zf.read(full)
            except KeyError:
                continue
            soup = BeautifulSoup(raw, "html.parser")
            _extract_images(
                soup, zf,
                xhtml_dir=str(Path(full).parent).replace("\\", "/"),
                opf_dir=opf_dir, images=images, counter=img_counter,
            )
            heading = soup.find(["h1", "h2", "h3"])
            headings.append(heading.get_text(strip=True) if heading else "")
            parts.append(soup.get_text("\n", strip=True) or "")

        # 某些 epub spine 缺失或为空，回退：取所有 xhtml 按文件名排序
        if not parts:
            html_files = sorted(
                name for name in zf.namelist()
                if name.lower().endswith((".xhtml", ".html", ".htm"))
                and not name.startswith("__MACOSX")
            )
            for name in html_files:
                soup = BeautifulSoup(zf.read(name), "html.parser")
                _extract_images(
                    soup, zf,
                    xhtml_dir=str(Path(name).parent).replace("\\", "/"),
                    opf_dir=opf_dir, images=images, counter=img_counter,
                )
                heading = soup.find(["h1", "h2", "h3"])
                headings.append(heading.get_text(strip=True) if heading else "")
                parts.append(soup.get_text("\n", strip=True) or "")

    # 4) 组装全文与章节偏移
    full_text = ""
    chapters: list[dict] = []
    offset = 0
    for i, text in enumerate(parts):
        chapters.append(
            {
                "index": i + 1,
                "title": headings[i][:_CHAPTER_TITLE_MAX] or f"第 {i + 1} 节",
                "char_start": offset,
                "char_end": offset + len(text),
            }
        )
        full_text += text
        if i < len(parts) - 1:
            full_text += "\n\n"
            offset += len(text) + 2
        else:
            offset += len(text)

    if not chapters:
        chapters = [{"index": 1, "title": "全文", "char_start": 0, "char_end": 0}]

    return ParsedBook(
        title=title or path.stem, author=author, chapters=chapters,
        full_text=full_text, images=images,
    )
