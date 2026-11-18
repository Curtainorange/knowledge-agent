"""全网找书：只记得情节 / 人物 / 作者，不记得书名时，靠联网检索把书定位出来。

为什么需要它：L1 的「按线索找回」只在**用户自己的知识库里**找。而最常见的真实情况是
——那本书他根本没存过，只记得一个片段。这时候知识库检索必然是空的，追问三轮也没有
意义：他要找的东西不在库里，在外面。

流程固定三步，每一步都能单独失败并如实说出来：

1. **规划检索词**（模型）：把「主角被困荒岛，用日记记下怎么活」变成几条能搜的词。
   不许先猜书名再用书名去搜——猜错一步，后面全在别的书上。
2. **联网检索**（DeepSeek 官方 Search，只读）：最多 3 条词、每条 6 条结果，按 URL 去重。
3. **判定**（模型）：从检索结果里挑出对得上的书，写清「你的哪条线索对上了它的哪一点」。

三条硬约定：

- **书名必须来自检索结果。** 候选要给出 `source_id`（S1/S2…），指不到任何一条结果的
  候选**直接丢弃**。这不是洁癖：编出来的书名、作者、出版年看起来和真的一模一样，
  用户照着去找才发现不存在，比「没查到」更糟。
- **搜不到就说搜不到。** `state` 区分 found / clarify / not_found / search_failed /
  unavailable / failed，不复用一张「大概是这本」的卡片糊过去。
- **不入库。** 本模块不 import 任何仓储；候选只活在这次回合里。用户点「记入知识库」
  才由既有录入链路写——他是过滤器，搜错一本不该变成他的知识。
  （`app/books/catalog` 是只读的离线书目文件，不是仓储：它只回答「哪有资源」，
  不写任何东西，也不进 prompt。）

task_type 复用 `l1_mining`（reasoning=on）：这仍然是「一点模糊线索 → 定位到具体对象」，
只是检索面从知识库换成了全网。不新开 task_type，避免无谓扩大 harness 登记面与成本口径。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.books import catalog
from app.llm import websearch
from app.llm.exceptions import LLMError
from app.llm.gateway import ModelGateway
from app.llm.prompts import BOOK_SEARCH_MATCH, BOOK_SEARCH_QUERY
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

# 预算。三个数字各管一件事：
# - 检索词条数管「角度够不够」（情节 / 作者 / 年代体裁各一条就够，再多是浪费）；
# - 每条结果数管上下文长度；
# - 候选书条数管卡片长度——列三本已经够用户判断了，列十本等于没挑。
MAX_QUERIES = 3
RESULTS_PER_QUERY = 6
MAX_RESULTS = 18
MAX_BOOKS = 3
MAX_HISTORY = 6

# 提示词里每条摘要的截断长度：判断「是不是这本书」看人物、情节、作者就够，
# 整段塞进去只会把真正的线索冲淡，还更贵。
SUMMARY_CHARS = 220


class BookQueries(BaseModel):
    """第一步的产出：几条检索词。"""

    queries: list[str] = Field(default_factory=list)


def _as_confidence(value) -> float:
    """把模型给的把握度收敛成 0..1。

    模型常常写「高」「中」或 "80%"，直接当 float 校验会让整次找书失败——
    一个展示用的数字不该有这种权力。
    """
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        num = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            return 0.0
        if text.endswith("%"):
            try:
                num = float(text[:-1]) / 100.0
            except ValueError:
                return 0.0
        else:
            try:
                num = float(text)
            except ValueError:
                return {"很高": 0.9, "高": 0.85, "中等": 0.6, "中": 0.6, "较低": 0.25, "低": 0.3}.get(
                    text, 0.0
                )
    return 0.0 if num < 0 else (1.0 if num > 1 else num)


class BookCandidate(BaseModel):
    """一条候选书。`source_id` 是必填证据：对不上检索结果就会被丢弃。"""

    title: str = ""
    author: str = ""
    year: str = ""
    why: str = ""
    confidence: float = 0.0
    source_id: str = ""

    @field_validator("title", "author", "year", "why", "source_id", mode="before")
    @classmethod
    def _text(cls, value) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("confidence", mode="before")
    @classmethod
    def _number(cls, value) -> float:
        return _as_confidence(value)


class BookMatch(BaseModel):
    """第三步的产出：结论 + 候选 + 一个（没找到时才用的）收敛问题。"""

    summary: str = ""
    books: list[BookCandidate] = Field(default_factory=list)
    question: str = ""

    @field_validator("summary", "question", mode="before")
    @classmethod
    def _text(cls, value) -> str:
        return "" if value is None else str(value).strip()


@dataclass
class FoundBook:
    title: str
    author: str = ""
    year: str = ""
    why: str = ""
    confidence: float = 0.0
    url: str = ""
    site: str = ""
    # ---- 去哪拿（来自离线目录，不是模型给的）----
    # 这几个字段永远不由模型填：模型只负责「是哪本书」，资源位置由本地书目查出来。
    # 混在一起会出事——模型编一个网盘链接，用户点进去发现是空的，比没查到更糟。
    resource_url: str = ""
    resource_category: str = ""
    resource_tags: list[str] = field(default_factory=list)
    has_epub: bool = False


@dataclass
class BookSearchResult:
    """一次找书的产物。`books` 只用于展示，不写库。"""

    state: str                       # found | clarify | not_found | search_failed | unavailable | failed
    books: list[FoundBook] = field(default_factory=list)
    summary: str = ""
    question: str = ""
    note: str = ""
    queries: list[str] = field(default_factory=list)
    searched: int = 0                # 拿到多少条网页结果（给用户一个「查过了」的量）

    @property
    def ok(self) -> bool:
        return self.state == "found"


def find_books(
    *,
    gateway: ModelGateway,
    session: Session,
    user_id: str,
    message: str,
    history: list[str] | None = None,
    exclude: list[str] | None = None,
) -> BookSearchResult:
    """跑一次「规划检索词 → 联网检索 → 判定」。不写任何数据。"""
    text = (message or "").strip()
    if not text:
        return BookSearchResult(state="not_found", note="先描述一下你记得的部分（情节、人物、作者都行）。")

    if not websearch.available():
        # 没 Key 就搜不了。这里也不调模型：让它凭记忆报书名正是要避免的事。
        return BookSearchResult(
            state="unavailable",
            note="联网检索没有配置好（缺 DEEPSEEK_API_KEY），这次只能停在这里。",
        )

    history_lines = _clean_history(history)
    exclude_titles = _clean_titles(exclude)

    queries = _plan_queries(gateway, session, user_id, text, history_lines)
    results = _search_all(queries)
    if not results:
        # 换一条更宽的词再试一次。这不是「重复同一个 query」，而是拿用户的原话兜底：
        # 模型给的词可能太窄，而搜索引擎对一整句中文描述其实容忍度不错。
        fallback = _fallback_query(text)
        if fallback and fallback not in queries:
            queries = [*queries, fallback]
            results = _search_all([fallback])
    if not results:
        return BookSearchResult(
            state="search_failed",
            queries=queries,
            note=f"搜了 {len(queries)} 条词，网页都没有结果（可能网络不通或被限流），这次没法判断。",
        )

    try:
        match = _match(
            gateway, session, user_id, text, history_lines, exclude_titles, results
        )
    except LLMError as exc:
        logger.warning("book_finder 判定失败: %s", exc)
        return BookSearchResult(
            state="failed",
            queries=queries,
            searched=len(results),
            note="检索到了网页，但这次没能判断出来（模型调用失败）。过一会儿再试一次。",
        )

    index = {f"S{i}": item for i, item in enumerate(results, 1)}
    books = _clean_books(match.books, index, exclude_titles)
    _attach_resources(books)

    if books:
        return BookSearchResult(
            state="found",
            books=books,
            summary=match.summary,
            queries=queries,
            searched=len(results),
        )

    question = (match.question or "").strip()
    if question:
        return BookSearchResult(
            state="clarify",
            question=question,
            queries=queries,
            searched=len(results),
            note=f"查了 {len(results)} 条网页，没有对得上的书。再补一点你记得的细节。",
        )
    return BookSearchResult(
        state="not_found",
        queries=queries,
        searched=len(results),
        note=f"查了 {len(results)} 条网页，没有对得上的书。换个说法、或补一点细节再找一次。",
    )


# ---- 内部 ------------------------------------------------------------------


def _attach_resources(books: list[FoundBook]) -> None:
    """给每本候选补一条「去哪拿」。原地修改，查不到就保持空。

    为什么放在 `find_books` 里而不是调用点：接口（`app/api/endpoints/l1.py`）与
    将来的对话能力都会调 `find_books`，放调用点会漏一处，两处口径随后必然分叉。

    查不到是常态——目录一万多条，包不住所有书。**没资源绝不改变找书结论**：
    「我确定是这本书」和「我这儿有它的下载」是两件事，前者成立、后者不成立很常见。
    """
    for book in books:
        try:
            hits = catalog.lookup(book.title, author=book.author, limit=1)
        except Exception as exc:  # 目录文件坏了不该把一次成功的找书打成 500
            logger.warning("资源目录查询失败 title=%r err=%s", book.title, exc)
            continue
        if not hits:
            continue
        entry = hits[0]
        book.resource_url = entry.link
        book.resource_category = entry.category
        # 标签全带上：源数据里一本书平均挂 2.1 个标签，只给第一个等于把分类砍掉一半。
        book.resource_tags = list(entry.categories)
        book.has_epub = entry.has_epub


def _clean_history(history: list[str] | None) -> list[str]:
    items = [str(h).strip() for h in (history or []) if str(h or "").strip()]
    return items[-MAX_HISTORY:]


def _clean_titles(titles: list[str] | None) -> list[str]:
    items: list[str] = []
    for raw in titles or []:
        title = _strip_title(str(raw or ""))
        if title and title not in items:
            items.append(title)
    return items


def _strip_title(raw: str) -> str:
    text = (raw or "").strip()
    for left, right in (("《", "》"), ("〈", "〉"), ("「", "」"), ("\"", "\""), ("'", "'")):
        if text.startswith(left) and text.endswith(right) and len(text) > 1:
            text = text[1:-1].strip()
    return text


def _norm_title(raw: str) -> str:
    return "".join(ch for ch in _strip_title(raw).lower() if not ch.isspace())


def _clean_queries(raw: list[str]) -> list[str]:
    queries: list[str] = []
    for item in raw:
        query = _strip_title(str(item or "").strip())
        query = query.strip("?？。.！!")
        if not query:
            continue
        query = query[:60]
        if query in queries:
            continue
        queries.append(query)
        if len(queries) >= MAX_QUERIES:
            break
    return queries


def _fallback_query(message: str) -> str:
    """用户原话裁一段当检索词。去掉换行，留够语义。"""
    line = " ".join((message or "").split())
    return line[:80]


def _plan_queries(
    gateway: ModelGateway,
    session: Session,
    user_id: str,
    message: str,
    history: list[str],
) -> list[str]:
    """第一步：把记忆片段变成检索词。失败就退到用户原话（不阻断整条链路）。"""
    messages = [
        {"role": "system", "content": BOOK_SEARCH_QUERY.text},
        {"role": "user", "content": _clue_block(message, history)},
        {"role": "user", "content": "请按上面的规矩给检索词，输出 JSON。"},
    ]
    try:
        completion = gateway.chat(
            task_type="l1_mining",
            messages=messages,
            user_id=user_id,
            session=session,
            prompt_version=BOOK_SEARCH_QUERY.version,
            json_model=BookQueries,
        )
        plan = parse_structured(completion.text, validator=lambda d: BookQueries(**d))
        queries = _clean_queries(plan.queries)
    except (LLMError, JsonParseError) as exc:
        logger.warning("book_finder 检索词规划失败，退到用户原话: %s", exc)
        queries = []
    return queries or [_fallback_query(message)]


def _search_all(queries: list[str]) -> list[dict]:
    """按 URL 去重后返回结果列表。搜索本身的失败由 websearch 收敛成 ok=False。"""
    seen: dict[str, dict] = {}
    for query in queries:
        result = websearch.search(query, count=RESULTS_PER_QUERY)
        if not result.get("ok"):
            logger.info("book_finder 检索无结果 query=%r error=%s", query, result.get("error"))
            continue
        for item in result.get("results") or []:
            url = str(item.get("url") or "").split("#", 1)[0]
            if not url:
                continue
            seen.setdefault(url, item)
            if len(seen) >= MAX_RESULTS:
                return list(seen.values())
    return list(seen.values())


def _clue_block(message: str, history: list[str]) -> str:
    if not history:
        return f"我记这本书的线索是：{message[:800]}"
    lines = [f"- {item[:200]}" for item in history]
    return (
        "我前面说过这些线索（越靠后越新）：\n"
        + "\n".join(lines)
        + f"\n\n这次我又补充：{message[:600]}"
    )


def _result_block(results: list[dict]) -> str:
    lines: list[str] = []
    for i, item in enumerate(results, 1):
        summary = " ".join(str(item.get("summary") or "").split())[:SUMMARY_CHARS]
        site = str(item.get("site") or "").strip()
        lines.append(f"[S{i}] {item.get('title') or ''}（{site}）\n    摘要：{summary}")
    return "\n".join(lines)


def _match(
    gateway: ModelGateway,
    session: Session,
    user_id: str,
    message: str,
    history: list[str],
    exclude_titles: list[str],
    results: list[dict],
) -> BookMatch:
    excluded = "、".join(f"《{title}》" for title in exclude_titles) or "（无）"
    body = (
        f"{_clue_block(message, history)}\n\n"
        f"已经看过、用户说不是的书：{excluded}\n\n"
        f"检索结果（只能从这些里面挑）：\n{_result_block(results)}\n\n"
        "请判断我记的是哪本书，输出 JSON。"
    )
    messages = [
        {"role": "system", "content": BOOK_SEARCH_MATCH.text},
        {"role": "user", "content": body},
    ]
    completion = gateway.chat(
        task_type="l1_mining",
        messages=messages,
        user_id=user_id,
        session=session,
        prompt_version=BOOK_SEARCH_MATCH.version,
        json_model=BookMatch,
    )
    # 解析失败会抛 JsonParseError（属 LLMError），由 find_books 统一收敛成 failed——
    # 「判不出来」与「没有对得上的书」是两件事，不能混成一句「没找到」。
    return parse_structured(completion.text, validator=lambda d: BookMatch(**d))


def _source_key(raw: str) -> str:
    """把各种写法的出处编号收敛成 `S1` 这种形状（`[s1]` / `1` / `S01` 都认）。"""
    text = str(raw or "").strip().strip("[]【】 ")
    if not text:
        return ""
    if text.isdigit():
        return f"S{int(text)}"
    upper = text.upper()
    if upper.startswith("S") and upper[1:].isdigit():
        return f"S{int(upper[1:])}"
    return upper


def _clean_books(
    candidates: list[BookCandidate],
    index: dict[str, dict],
    exclude_titles: list[str],
) -> list[FoundBook]:
    """收敛候选：去重、剔除已看过的、**丢弃指不到检索结果的**。"""
    excluded = {_norm_title(title) for title in exclude_titles}
    books: list[FoundBook] = []
    seen: set[str] = set()

    for candidate in candidates:
        title = _strip_title(candidate.title)
        if not title or len(title) > 80:
            continue
        key = _norm_title(title)
        if key in seen or key in excluded:
            continue
        source = index.get(_source_key(candidate.source_id))
        if source is None:
            # 无出处的候选一律丢弃：它极可能来自模型记忆而不是这次检索。
            logger.info("book_finder 丢弃无出处候选：%s（source_id=%r）", title, candidate.source_id)
            continue
        seen.add(key)
        books.append(
            FoundBook(
                title=title,
                author=candidate.author[:40],
                year=candidate.year[:16],
                why=candidate.why[:160],
                confidence=round(candidate.confidence, 2),
                url=str(source.get("url") or ""),
                site=str(source.get("site") or ""),
            )
        )
        if len(books) >= MAX_BOOKS:
            break
    return books
