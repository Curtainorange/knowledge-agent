"""离线资源目录：把「书名」变成「去哪拿」。

定位：`app/agent/book_finder.py` 解决的是**认书**（只记得情节 → 定位书名），
它给的是名字，用户还得自己去搜资源。这一层解决**拿书**——一份 2.4 万条的
离线书目，按书名/作者查出网盘链接与可用格式，零出网、零 token。

为什么是离线 JSON 而不是实时检索：
数据源（jbiaojerry/ebook-treasure-chest 的 `docs/all-books.json`）本身就是静态文件，
联网去读它只是把一次本地查找换成一次网络请求，还多一个会挂的依赖。索引由
`scripts/import_book_catalog.py` 手动导入到 `data/book_catalog.json`（`/data/` 已 git-ignored，
7MB 不入库），运行时不联网。

三条边界，别越：

1. **只给链接，不代下。** 链接指向城通网盘分享页，真实下载地址由页面 JS 动态换取，
   纯 HTTP 拿不到；且这批是未授权电子书。自动下载不在本层职责内。
2. **必须暴露格式。** 上传白名单只认 `.txt / .epub / .pdf`，而目录给的是
   epub / mobi / azw3 三种。对只有 mobi 的条目说「有资源」，等于让人下一份进不了阅读器的文件。
3. **查不到就说查不到。** 匹配不到返回空列表，不糊一条「大概是这本」——
   和 book_finder 同一条纪律：编出来的书名比「没查到」更糟。
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根 / data / book_catalog.json（app/books/catalog.py → parents[2] 是项目根）
CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "book_catalog.json"

# 原始数据里的占位值：链接字段填的是这五个字，不是 URL。
_MISSING_LINK = "链接未找到"

# 打分只用于**排序**，不是概率。用户看到的是「有/没有」，看不到分数。
#
# 四档的间距是有来由的，别随手调平：真机实测查「刻意练习」时，第一条给的是
# 「天才源自刻意练习」，查「人类简史」给的是「人类酷刑简史」——都是因为
# 「字符相近」与「包含」拿了同一个分数，然后按长度排，短的那条赢了。
# 用户要的是「以他说的那本开头的书」，不是「字数最少的相似书」。
_SCORE_EXACT = 1.0
_SCORE_PREFIX = 0.92      # 书名以查询开头：最像目标书
_SCORE_CONTAINS = 0.85    # 查询夹在书名中间 / 反向包含：相关，但不是它
_SCORE_SIMILARITY_CAP = 0.75  # 兜底档，**永远低于包含**，只用来救错别字与简繁差异
_SCORE_AUTHOR_BONUS = 0.1
# 标签命中的加分。比作者小：标签是粗分类，作者是硬身份。
# 它只在「分数已经过线」之后生效，用来分开重名书，不会把不相干的书拉进来。
_SCORE_TAG_BONUS = 0.05
# 相似度门槛：低于它宁可返回空。中文短书名之间 difflib 容易误伤
# （「心理学」vs「理学」能到 0.8），所以只接受真的改了几个字的情况。
_SIMILARITY_THRESHOLD = 0.72
# 包含匹配的最短长度：一个字的名字（「书」「传」）会把半架书都捞出来。
_MIN_PARTIAL_LEN = 2


@dataclass(frozen=True)
class ResourceEntry:
    """一条资源。`formats` 是网盘里实际提供的格式，`categories` 是这本书挂的全部标签。

    一本书有多个标签是**正常**的，不是脏数据：源仓库的 `md/` 按标签组织，
    《思考，快与慢》既在「认知」也在「心理学」，导出成全量索引就是两行。
    实测每本书平均 2.2 个标签（1 个 2,457 本 / 2 个 5,440 / 3 个 3,156 / 4 个 280）。
    所以载入时按链接合并、标签全留，绝不能按「书名+作者+链接」去重——
    那样每本书会被静默丢掉 1.2 个标签。
    """

    title: str
    author: str = ""
    link: str = ""
    categories: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()

    @property
    def category(self) -> str:
        """主标签。给只认单个标签的旧调用方（找书卡片）用，完整标签看 `categories`。"""
        return self.categories[0] if self.categories else ""

    @property
    def has_epub(self) -> bool:
        """能不能直接进阅读器。只有 epub（以及 txt/pdf）在上传白名单里。"""
        return any(f.strip().upper() == "EPUB" for f in self.formats)


def normalize(text: str) -> str:
    """比对用的归一化：全角折叠、大小写折叠、去掉所有标点与空白。

    NFKC 负责全角 → 半角（`ＰＹＴＨＯＮ` → `PYTHON`），也让「·」这类间隔号
    归到一个稳定形态；剩下的书名号、冒号、括号统一由正则剥掉。
    """
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", folded)


def _score(entry: ResourceEntry, query: str, author_query: str) -> float:
    title = normalize(entry.title)
    if not title:
        return 0.0

    if title == query:
        base = _SCORE_EXACT
    elif title.startswith(query) and len(query) >= _MIN_PARTIAL_LEN:
        # 主标题在前：用户说「人类简史」，「人类简史（知识漫画）」优先。
        base = _SCORE_PREFIX
    elif (
        (query in title or title in query)
        and min(len(title), len(query)) >= _MIN_PARTIAL_LEN
    ):
        # 查询夹在中间（「天才源自刻意练习」）或用户念得更全（带副标题）：
        # 相关，但不是目标书本身。
        base = _SCORE_CONTAINS
    else:
        ratio = difflib.SequenceMatcher(None, query, title).ratio()
        # 乘 0.9 再封顶：凑巧字符相近的书，排序必须落在这两条之下。
        base = min(_SCORE_SIMILARITY_CAP, ratio * 0.9) if ratio >= _SIMILARITY_THRESHOLD else 0.0

    if base and author_query and author_query in normalize(entry.author):
        # 重名书靠作者分开。不封顶 1.0 —— 分数只是排序键，封顶会让作者命中失去意义。
        base += _SCORE_AUTHOR_BONUS
    return base


def search(
    entries: list[ResourceEntry],
    title: str,
    *,
    author: str = "",
    tags: Sequence[str] = (),
    limit: int = 3,
) -> list[ResourceEntry]:
    """在给定条目里查书名。空查询、查不到、limit<=0 都返回空列表。

    `tags` **只加分、不做过滤**：用户给的标签未必在目录的词表里（「社科」这种宽泛说法
    目录里没有），用它过滤会把唯一正确的那条杀掉。重名书靠它分先后即可。
    """
    query = normalize(title)
    if not query or limit <= 0:
        return []
    author_query = normalize(author)
    tag_queries = _normalize_tags(tags)

    scored: list[tuple[float, int, int, ResourceEntry]] = []
    for entry in entries:
        score = _score(entry, query, author_query)
        if score <= 0:
            continue
        if tag_queries and _tag_hits(entry, tag_queries):
            score += _SCORE_TAG_BONUS
        # 同分时：先 epub（能直接读），再短的（更像正式书名，不像合集）。
        scored.append((score, 0 if entry.has_epub else 1, len(normalize(entry.title)), entry))

    scored.sort(key=lambda row: (-row[0], row[1], row[2], row[3].title))
    return [row[3] for row in scored[:limit]]


def browse(
    entries: list[ResourceEntry],
    *,
    tags: Sequence[str] = (),
    limit: int = 10,
) -> list[ResourceEntry]:
    """按标签挑书——用户说不出书名、只说了个方向时（「找本心理学的书」）。

    排序：命中标签数多的优先，其次 epub，剩下**保持目录原始顺序**。
    刻意不按书名长度排：那会让「偏见」「坚毅」这类冷门短名排在《思考，快与慢》前面
    ——数据里没有热度字段，与其用一个看似合理的代理指标挑错，不如老实按原序给一批候选，
    让用户自己挑。`limit` 默认给到 10 也是这个原因：这一步的产出是候选集，不是答案。
    """
    tag_queries = _normalize_tags(tags)
    if not tag_queries or limit <= 0:
        return []

    scored: list[tuple[int, int, int, ResourceEntry]] = []
    for position, entry in enumerate(entries):
        hits = _tag_hits(entry, tag_queries)
        if not hits:
            continue
        scored.append((-hits, 0 if entry.has_epub else 1, position, entry))

    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in scored[:limit]]


def _normalize_tags(tags: Sequence[str] | None) -> list[str]:
    out: list[str] = []
    for tag in tags or ():
        text = normalize(tag)
        if len(text) >= _MIN_PARTIAL_LEN and text not in out:
            out.append(text)
    return out


def _tag_hits(entry: ResourceEntry, tag_queries: Sequence[str]) -> int:
    """命中几个标签。互相包含就算命中——用户说「心理」，目录里是「心理学」。"""
    own = [normalize(c) for c in entry.categories]
    return sum(1 for q in tag_queries if any(q in c or c in q for c in own))


# 载入缓存。按路径分别缓存：测试里会喂 tmp 路径，不能互相污染。
_CACHE: dict[Path, list[ResourceEntry]] = {}


def _tags(item: dict) -> list[str]:
    """取一本书的全部标签。新格式是 `categories` 数组，老格式是 `category` 字符串。"""
    raw = item.get("categories")
    if not isinstance(raw, list):
        raw = [item.get("category")] if item.get("category") else []
    tags: list[str] = []
    for tag in raw:
        text = str(tag or "").strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def _formats(item: dict) -> list[str]:
    raw = item.get("formats")
    if not isinstance(raw, list):
        return []
    return [str(f).strip() for f in raw if str(f).strip()]


def _read(path: Path) -> list[ResourceEntry]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # `data/` 是 git-ignored：干净检出、换机器时文件本来就不存在，这不算错误。
        return []
    except (OSError, ValueError) as exc:
        logger.warning("book catalog unreadable path=%s err=%s", path, exc)
        return []

    if not isinstance(raw, list):
        logger.warning("book catalog has unexpected shape path=%s type=%s", path, type(raw).__name__)
        return []

    # 按链接合并：同一本书挂在多个标签下会重复出现，合并成一条、标签取并集。
    # 用 dict 存中间态而非直接 append，是因为这一步的目的就是**不丢标签**；
    # 顺带让「data 文件里还有重复行」也不会变成同一本书出现三次。
    slots: dict[str, dict] = {}
    order: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        # 无链接的条目是死数据（原始数据里有一批字面量占位）。
        if not link or link == _MISSING_LINK:
            continue
        title = str(item.get("title") or "").strip()
        author = str(item.get("author") or "").strip()

        slot = slots.get(link)
        if slot is None:
            slots[link] = {
                "title": title,
                "author": author,
                "categories": _tags(item),
                "formats": _formats(item),
            }
            order.append(link)
            continue

        # 后续行只补空缺，不覆盖：先到的那行是这个链接的原始记录。
        if not slot["title"] and title:
            slot["title"] = title
        if not slot["author"] and author:
            slot["author"] = author
        for tag in _tags(item):
            if tag not in slot["categories"]:
                slot["categories"].append(tag)
        for fmt in _formats(item):
            if fmt not in slot["formats"]:
                slot["formats"].append(fmt)

    entries: list[ResourceEntry] = []
    for link in order:
        slot = slots[link]
        # 合并完仍无书名的条目检索不到，只能丢。
        if not slot["title"]:
            continue
        entries.append(
            ResourceEntry(
                title=slot["title"],
                author=slot["author"],
                link=link,
                categories=tuple(slot["categories"]),
                formats=tuple(slot["formats"]),
            )
        )
    return entries


def load(path: Path | None = None) -> list[ResourceEntry]:
    """载入目录（按路径缓存）。文件缺失/损坏都退化成空列表，绝不抛。"""
    target = Path(path) if path is not None else CATALOG_PATH
    cached = _CACHE.get(target)
    if cached is not None:
        return cached
    entries = _read(target)
    _CACHE[target] = entries
    return entries


def lookup(
    title: str,
    *,
    author: str = "",
    tags: Sequence[str] = (),
    limit: int = 3,
) -> list[ResourceEntry]:
    """查一条书的可用资源。查不到返回空列表。"""
    return search(load(), title, author=author, tags=tags, limit=limit)


def browse_catalog(*, tags: Sequence[str], limit: int = 10) -> list[ResourceEntry]:
    """按标签从整个目录里挑书（书名未知时用）。"""
    return browse(load(), tags=tags, limit=limit)


def reset_cache() -> None:
    """丢掉缓存。导入脚本更新数据后、测试之间需要它。"""
    _CACHE.clear()
