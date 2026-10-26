"""书籍解析：.txt / .epub / .pdf → (标题, 作者, 章节结构, 全文)。

- txt：探测编码（utf-8 → gb18030 兜底），按章节标题正则切分。
- epub：标准 OCF 容器（本质是 zip）→ 读 container.xml 定位 OPF → 按 spine 顺序
  把每个 xhtml 文档当作一章，用 BeautifulSoup 抽取标题与纯文本。
  全程零第三方 epub 依赖（只用标准库 zipfile/xml + 已装依赖 bs4）。
- pdf：用 pypdf 逐页抽取文本（纯 Python，无二进制依赖）。PDF 本身没有章节结构，
  因此优先用内置书签（outline）分章；没有书签就按页分章（标题「第 N 页」）。
  抽出来的文本还要额外洗两遍：剔除跨页重复的页眉/页脚，合并被硬换行截断的段落。
"""
from __future__ import annotations

import posixpath
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
    # PDF 专有：每一物理页在 full_text 里的起点
    # [{page, char_start, label}]，page 从 1 起，label 是页面上原印的页码（可能为空）。
    # 为什么单独存而不在正文插标记：插标记会把所有 char 偏移顶歪，
    # 已有的高亮 / 摘录 / 通读分块全都按 char_start 存，改一次就得全量迁移。
    page_marks: list[dict] = field(default_factory=list)


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
    """把 href 解析成 zip 内的真实条目名。

    **必须做路径规范化**：epub 里图片通常写成 `../images/pic.jpg`（相对当前 xhtml），
    而 zip 内的条目名是 `images/pic.jpg`。不解析 `..` 就会拼出
    `text/../images/pic.jpg` 这种库里不存在的名字，`zf.read` 抛 KeyError，
    图片被当成「读不到外链图」整批丢弃——整本图画书只剩零星文字。
    """
    if href.startswith("/"):
        return posixpath.normpath(href).lstrip("/")
    if opf_dir in ("", "."):
        return posixpath.normpath(href)
    return posixpath.normpath(posixpath.join(opf_dir, href))


def _visible_text(soup) -> str:
    """取可见正文：先摘掉 head/script/style，再取文本。

    `soup.get_text()` 会把 `<head><title>` 也算进来。Calibre 转出的 epub 里有大量
    `<title>未知</title>`，直接取全文会让每章开头凭空多一行「未知」——读者以为正文
    坏了，其实是标题标签混进了正文。
    """
    for tag in soup.find_all(["head", "script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True) or ""


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


def parse_epub(path: Path, title: str | None = None) -> ParsedBook:
    with zipfile.ZipFile(path) as zf:
        # 1) container.xml → OPF 路径
        container = ET.fromstring(zf.read("META-INF/container.xml"))
        rootfile = next(el for el in container.iter() if el.tag.endswith("rootfile"))
        opf_path = rootfile.get("full-path", "")
        opf_dir = str(Path(opf_path).parent).replace("\\", "/")

        # 2) OPF → 元数据 / manifest / spine
        opf_root = _strip_namespace(ET.fromstring(zf.read(opf_path)))
        meta_title = author = ""
        manifest: dict[str, str] = {}
        spine: list[str] = []
        for el in opf_root.iter():
            tag = el.tag
            if tag == "title" and not meta_title:
                meta_title = (el.text or "").strip()
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
            text = _visible_text(soup)
            # 空文档（封面页常见：body 里只有一张图或什么都没有）不生成章节，
            # 否则第一章打开就是一片空白，看起来像书坏了。
            if not text.strip():
                continue
            headings.append(heading.get_text(strip=True) if heading else "")
            parts.append(text)

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
                text = _visible_text(soup)
                if not text.strip():
                    continue
                headings.append(heading.get_text(strip=True) if heading else "")
                parts.append(text)

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
        title=meta_title or (title or "").strip() or path.stem,
        author=author, chapters=chapters,
        full_text=full_text, images=images,
    )


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #

# 逐页抽文本会长时间占住请求线程，页数过多时宁可明确拒绝
_PDF_MAX_PAGES = 2000

# 页眉/页脚（统称「页眉装饰」）剔除：只在每页首行 / 末行里找跨页重复的行。
# 范围刻意只取一行：漏删一个页眉只是难读，误删正文却是内容丢失，宁可保守。
_PDF_FURNITURE_BAND = 1          # 只考察页首 1 行 / 页末 1 行
_PDF_FURNITURE_MAX_LEN = 40      # 超过这个长度的行不参与判定，正文长句不会被误删
_PDF_FURNITURE_MIN_PAGES = 3     # 至少要在这么多页上重复
_PDF_FURNITURE_RATIO = 0.6       # 且覆盖 ≥60% 的页
_PDF_FURNITURE_MIN_TOTAL = 6     # 总页数不足 6 页时一律不剔，避免短文被误伤

# 软换行合并：行尾无句末标点且已足够长 → 判定为被 PDF 硬换行截断
_PDF_WRAP_MIN_LEN = 15
# 连续多少个「单字行」才认定是逐字定位排版（而不是真的标题 / 诗句）
_PDF_SINGLE_CHAR_RUN = 3
_PDF_TERMINATORS = "。！？；：…!?;:.）)】》」』”’"
# 这些开头的行属于新块（列表项 / 项目符号 / 章节标题 / 缩进段落），不与上一行合并
_PDF_BLOCK_START = re.compile(
    r"^\s*(?:"
    r"[\d０-９]+\s*[.、)）]"                                  # 1. / 2、/ 3)
    r"|[•·※*\-—+✓□■◆○●▲]"                                 # 项目符号
    r"|第[\d一二三四五六七八九十百千零两]+[章节回篇部讲课]"   # 章节标题
    r"|\u3000"                                               # 全角缩进 = 新段落
    r")"
)

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])")
# 纯页码行：页眉页脚剔除没生效时（页数太少等），至少别把它并进正文句子
_PDF_PAGE_NUMBER_RE = re.compile(r"[\d０-９]{1,6}")
# 前言 / 目录常用罗马数字编页（i, ii, xiii）
_PDF_ROMAN_RE = re.compile(r"[ivxlcdm]{1,7}", re.I)
# 印刷页码的最大长度：超过这个长度的「数字行」更可能是正文里的数据
_PDF_LABEL_MAX_LEN = 8
# 同一个标签出现在超过这个比例的页上，说明它不是页码（每页都印的年份之类）
_PDF_LABEL_MAX_PAGE_RATIO = 0.3

# PDF 元数据里的常见占位值：直接当书名会让整架书都显示成 untitled
_PDF_JUNK_METADATA = {
    "", "untitled", "untitled document", "unknown", "anonymous", "document",
    "document1", "pdf", "microsoft word", "无标题", "未命名", "未命名文档", "新建文档",
}
_PDF_JUNK_SUFFIXES = (".doc", ".docx", ".pdf", ".indd", ".qxd", ".ppt", ".pptx")


class _PdfError(ValueError):
    """PDF 解析中「可以直接展示给用户」的错误。

    与底层库抛出的异常区分开：只有这类错误才把原文当作提示语给用户，
    其余一律包成「解析 PDF 失败」并附带原因。
    """


def _pdf_reader(path: Path):
    """打开 PDF。pypdf 缺失时给出可读提示，而不是让 ImportError 冒到接口层。"""
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - 仅在依赖未安装的环境触发
        raise _PdfError("缺少 PDF 解析依赖 pypdf，请先执行 pip install pypdf") from exc
    try:
        return PdfReader(str(path))
    except Exception as exc:
        raise _PdfError(f"无法打开 PDF：{exc}") from exc


def _clean_pdf_metadata(value: str) -> str:
    """过滤掉「一眼就是占位值」的 PDF 元数据，让书名回退到文件名。

    现实里大量 PDF 的 Info 字典是导出工具写死的 untitled / anonymous /
    Microsoft Word，直接采纳会让整个书架都叫「untitled」。
    """
    text = (value or "").strip()
    if not text or text.lower() in _PDF_JUNK_METADATA:
        return ""
    if text.lower().endswith(_PDF_JUNK_SUFFIXES):
        return ""
    return text


def _pdf_metadata(reader) -> tuple[str, str]:
    """读 PDF 元数据里的标题 / 作者；读不到或明显是占位值就当没有。"""
    try:
        info = reader.metadata
    except Exception:
        return "", ""

    def _get(key: str) -> str:
        if not info:
            return ""
        try:
            value = info.get(key) if hasattr(info, "get") else getattr(info, key, None)
        except Exception:
            return ""
        return str(value).strip() if value else ""

    return _clean_pdf_metadata(_get("/Title"))[:200], _clean_pdf_metadata(_get("/Author"))[:128]


def _pdf_page_text(page) -> str:
    """取一页的文本；个别页损坏时退化为空页，不拖垮整本。"""
    try:
        return page.extract_text() or ""
    except Exception:
        return ""


def _squeeze_cjk_spaces(line: str) -> str:
    """压掉「汉字 空格 汉字」式伪空格。

    PDF 里每个字都是独立定位绘制的，抽文本时常在汉字之间插进空格
    （「这 是 一 句 话」）。只在空格切出的碎片绝大多数是单字汉字时才压缩，
    否则会破坏「用 Python 写脚本」这类正常的中英混排。
    """
    tokens = line.split()
    if len(tokens) < 4:
        return line
    singles = [t for t in tokens if len(t) == 1 and _CJK_RE.match(t)]
    if len(singles) * 3 < len(tokens) * 2:  # 单字汉字占比不足 2/3
        return line
    return _CJK_SPACE_RE.sub("", line)


def _furniture_signature(line: str) -> str:
    """页眉在每页上的差异通常只有页码，归一化时去掉数字与空白。"""
    signature = re.sub(r"[\s\d０-９\u3000]+", "", line)
    return signature or "\x00page-number"  # 纯数字行（页码）单独归一档


def _furniture_positions(lines: list[str]) -> list[tuple[int, list[int]]]:
    """页首 / 页末各行的下标（band：0 = 页首，1 = 页末）。"""
    top = list(range(min(_PDF_FURNITURE_BAND, len(lines))))
    bottom = list(range(max(0, len(lines) - _PDF_FURNITURE_BAND), len(lines)))
    return [(0, top), (1, bottom)]


def _pdf_page_label(line: str) -> str:
    """把一行「像页码的东西」抽成裸数字（印刷页码），不像就返回空。

    容忍 `123` / `- 123 -` / `第 123 页` / `iv` / `xiii` 这几种常见印法：
    先去掉页/破折号/空白等噪音字符，再看剩下的整串是不是纯数字或罗马数字。
    """
    core = re.sub(r"[第页\s—–\-·.]", "", line)
    if not core or len(core) > _PDF_LABEL_MAX_LEN:
        return ""
    if _PDF_PAGE_NUMBER_RE.fullmatch(core) or _PDF_ROMAN_RE.fullmatch(core):
        return core
    return ""


def _printed_page_labels(raw_pages: list[str]) -> dict[int, str]:
    """从每页首 / 末行认出印刷页码 → {页下标: 标签}。

    页码行本来会被 `_strip_page_furniture` 当页脚剔掉（短行、去掉数字后每页签名相同），
    但「第 N 页」正是读者对照纸书时唯一认得的坐标。先捞出来再剔：正文干净，坐标留着。

    先看页末（页码通常在页脚），页末不像页码再看页首。页数太少时一律不认——
    短文档里首行的纯数字更可能是正文数据。
    """
    total = len(raw_pages)
    if total < _PDF_FURNITURE_MIN_TOTAL:
        return {}

    picked: dict[int, str] = {}
    for page_no, text in enumerate(raw_pages):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        label = _pdf_page_label(lines[-1]) or _pdf_page_label(lines[0])
        if label:
            picked[page_no] = label

    # 页码逐页不同：同一个「标签」出现在太多页上，说明它是每页都印的东西（年份、书名编号）
    counts: dict[str, int] = {}
    for label in picked.values():
        counts[label] = counts.get(label, 0) + 1
    limit = max(2, int(total * _PDF_LABEL_MAX_PAGE_RATIO))
    return {page_no: label for page_no, label in picked.items() if counts[label] <= limit}


def _strip_page_furniture(pages: list[list[str]]) -> list[list[str]]:
    """剔除每页重复出现的页眉 / 页脚（书名、章名、页码）。

    判定范围只限每页首尾各两行，且要求同一签名覆盖足够多的页——正文里偶然重复
    的语句不会刚好都落在页首尾，因此误伤概率很低；页数太少的文档直接跳过。
    """
    total = len(pages)
    if total < _PDF_FURNITURE_MIN_TOTAL:
        return pages

    occurrences: dict[tuple[str, int], set[int]] = {}
    for page_no, lines in enumerate(pages):
        for band, positions in _furniture_positions(lines):
            for pos in positions:
                line = lines[pos].strip()
                if not line or len(line) > _PDF_FURNITURE_MAX_LEN:
                    continue
                occurrences.setdefault((_furniture_signature(line), band), set()).add(page_no)

    threshold = max(_PDF_FURNITURE_MIN_PAGES, int(total * _PDF_FURNITURE_RATIO))
    repeated = {key for key, page_nos in occurrences.items() if len(page_nos) >= threshold}
    if not repeated:
        return pages

    cleaned: list[list[str]] = []
    for lines in pages:
        drop: set[int] = set()
        for band, positions in _furniture_positions(lines):
            for pos in positions:
                line = lines[pos].strip()
                if not line or len(line) > _PDF_FURNITURE_MAX_LEN:
                    continue
                if (_furniture_signature(line), band) in repeated:
                    drop.add(pos)
        cleaned.append([line for i, line in enumerate(lines) if i not in drop])
    return cleaned


def _needs_space(left: str, right: str) -> bool:
    """左右都是拉丁字母 / 数字才补空格，中文直接相接。"""
    return (
        left[-1].isascii() and left[-1].isalnum()
        and right[0].isascii() and right[0].isalnum()
    )


def _join_wrapped(prev: str, nxt: str) -> str:
    left = prev.rstrip()
    right = nxt.strip()
    if left.endswith("-") and not left.endswith("--"):
        return left[:-1] + right  # 英文行尾断词：去掉连字符直接相接
    if _needs_space(left, right):
        return left + " " + right
    return left + right


def _should_join(prev: str, nxt: str) -> bool:
    """上一行是「没写完的一行」时才与下一行相接。

    三种情况保持断开：行尾已是句末标点；上一行本身很短（标题 / 诗句 / 表格行）；
    下一行看起来是新块（列表项、章节标题、缩进段落）。
    """
    stripped = prev.rstrip()
    if not stripped or stripped[-1] in _PDF_TERMINATORS:
        return False
    if len(stripped) < _PDF_WRAP_MIN_LEN:
        return False
    return not _PDF_BLOCK_START.match(nxt)


def _is_single_char(line: str) -> bool:
    """整行只有一个可见字符。"""
    return len(line) == 1 and not line.isspace()


def _merge_wrapped_lines(lines: list[str]) -> list[str]:
    """把 PDF 抽文本留下的三类割裂接回去。

    1. 逐字定位排版造成的「一个字一行」—— 连续多个单字行接成一整行；
    2. 硬换行拆断的段落 —— 上一行没写完就与下一行相接；
    3. 汉字之间的伪空格 —— 交给 `_squeeze_cjk_spaces` 压掉。
    """
    cleaned = [_squeeze_cjk_spaces(raw.rstrip()) for raw in lines]
    out: list[str] = []
    i = 0
    while i < len(cleaned):
        line = cleaned[i]
        if not line.strip():
            if out and out[-1] != "":
                out.append("")  # 连续空行折叠成一个，用作段落分隔
            i += 1
            continue

        end = i
        while end < len(cleaned) and _is_single_char(cleaned[end]):
            end += 1
        if end - i >= _PDF_SINGLE_CHAR_RUN:
            out.append("".join(cleaned[i:end]))
            i = end
            continue

        if out and out[-1] and _should_join(out[-1], line):
            out[-1] = _join_wrapped(out[-1], line)
        else:
            out.append(line)
        i += 1
    return out


def _pdf_outline_marks(reader, page_count: int) -> list[tuple[str, int]]:
    """展平 PDF 内置书签 → [(标题, 起始页下标)]，按页序去重。

    没有书签、或书签结构读不出来（损坏 / 非常规写法）时返回空表，
    调用方据此退回「按页分章」。
    """
    try:
        outline = reader.outline
    except Exception:
        return []
    if not outline:
        return []

    collected: list[tuple[str, int]] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for child in node:
                walk(child)
            return
        try:
            title = str(node.get("/Title") or "").strip()
        except Exception:
            return
        if not title:
            return
        try:
            page_no = reader.get_destination_page_number(node)
        except Exception:
            return
        if not isinstance(page_no, int) or not 0 <= page_no < page_count:
            return
        collected.append((title[:_CHAPTER_TITLE_MAX], page_no))

    walk(outline)

    marks: list[tuple[str, int]] = []
    seen: set[int] = set()
    for _, (title, page_no) in sorted(enumerate(collected), key=lambda pair: (pair[1][1], pair[0])):
        if page_no in seen:
            continue  # 同一页挂了多个书签时，只保留最靠前的那个
        seen.add(page_no)
        marks.append((title, page_no))
    return marks


def _assemble_pdf(
    pages_text: list[str],
    marks: list[tuple[str, int]],
    labels: dict[int, str] | None = None,
) -> tuple[list[dict], str, list[dict]]:
    """按 (标题, 起始页) 分组拼出章节与全文；偏移规则与 epub 保持一致。

    顺便记下每页在全文里的起点（`page_marks`），阅读页据此显示「原书第 N 页」。
    逐页拼接而不是先 `join` 再算偏移：页边界只有拼的时候才知道，
    事后再用长度反推，页间分隔符规则一改就会全歪。
    """
    labels = labels or {}
    if marks:
        groups = list(marks)
        if groups[0][1] > 0:
            groups.insert(0, ("开篇", 0))  # 首个书签之前的封面 / 版权页
    else:
        groups = [(f"第 {i + 1} 页", i) for i in range(len(pages_text))]

    parts: list[str] = []
    chapters: list[dict] = []
    page_marks: list[dict] = []
    offset = 0
    for i, (title, start) in enumerate(groups):
        end = groups[i + 1][1] if i + 1 < len(groups) else len(pages_text)
        chapter_start = offset
        for page_no in range(start, end):
            if page_no > start:
                parts.append("\n\n")
                offset += 2
            page_marks.append(
                {"page": page_no + 1, "char_start": offset, "label": labels.get(page_no, "")}
            )
            parts.append(pages_text[page_no])
            offset += len(pages_text[page_no])
        chapters.append(
            {"index": i + 1, "title": title, "char_start": chapter_start, "char_end": offset}
        )
        if i < len(groups) - 1:
            parts.append("\n\n")
            offset += 2

    return chapters, "".join(parts), page_marks


def parse_pdf(path: Path, title: str | None = None) -> ParsedBook:
    """解析 PDF：优先按内置书签分章，没有书签就按页分章。"""
    try:
        reader = _pdf_reader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise _PdfError("这个 PDF 已加密，需要密码才能打开")
        page_count = len(reader.pages)
        if page_count == 0:
            raise _PdfError("这个 PDF 没有页面")
        if page_count > _PDF_MAX_PAGES:
            raise _PdfError(f"PDF 页数过多（{page_count} 页，上限 {_PDF_MAX_PAGES} 页）")
        meta_title, meta_author = _pdf_metadata(reader)
        marks = _pdf_outline_marks(reader, page_count)
        raw_pages = [_pdf_page_text(page) for page in reader.pages]
    except _PdfError:
        raise
    except Exception as exc:
        raise _PdfError(f"解析 PDF 失败：{exc}") from exc

    # 印刷页码必须在剔除页眉页脚**之前**取：页码行本身就会被当成页脚删掉
    labels = _printed_page_labels(raw_pages)
    pages = _strip_page_furniture(
        [[line.rstrip() for line in text.splitlines()] for text in raw_pages]
    )
    pages_text = ["\n".join(_merge_wrapped_lines(lines)).strip() for lines in pages]

    if not any(pages_text):
        raise _PdfError("这个 PDF 里没有可提取的文字（多为扫描图片版），暂时无法阅读")

    chapters, full_text, page_marks = _assemble_pdf(pages_text, marks, labels)
    return ParsedBook(
        title=meta_title or (title or "").strip() or path.stem,
        author=meta_author,
        chapters=chapters,
        full_text=full_text,
        page_marks=page_marks,
    )
