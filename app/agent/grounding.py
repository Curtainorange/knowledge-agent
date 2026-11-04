"""依据装配与校验（A 模块）：决定「哪条已同步内容有资格作为建议依据」。

这是「未入库内容不得作为建议依据」的唯一落地处。上层（L1/L3/L5/L4 与对话建议）
**不允许自己拼证据**，一律经这里取，避免同一条规则在几处各写一遍、口径静默分叉。

## 三级可用性

| 级别 | 含义 | 能否作为依据 |
|---|---|---|
| l1 | 已入库（`is_deleted=False`） | 否——只是"库里有" |
| l2 | 可召回（有向量或被关键词命中） | 否——召回得到但引不了 |
| l3 | 可溯源（quote 命中原文 **且** 能跳回定位） | **是**，只有 l3 能进 `basis_item_ids` |

为什么要有 l2 这个中间态：条目存在、语义检索也能召到，但抽不出可用片段（正文太短、
全是标点）时，引用它等于引用了一句空话。**允许召回、不允许引用**是这个级别的全部意义。

## relevance 的口径（容易踩的坑）

`Retriever.retrieve()` 返回的 `score` 是 **RRF 融合分**（约 `w/(k+rank)`，量级 0.0x），
不是余弦相似度。拿它跟 0.55 这种余弦阈值比，会永远不命中。
所以本模块的流程是：**用 Retriever 召回候选（保留它的双通道与降级能力），
再用落库向量重算真实余弦**作为 `relevance`。多算 ≤K 次余弦的代价，换阈值语义准确。

## 依据分级（与验收清单第 4/7 条对应）

- `library`：用户自己的知识条目（划线 / 笔记 / 手记）。**每条建议至少 1 条**。
- `agent_summary`：智能体通读笔记。可引用，但**不得单独构成依据**（它不是用户同步的内容）。
- `external`：外部推荐。**永不进本模块**，只出现在卡片 `references[]` 隔离区。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.knowledge_item import KnowledgeItem
from app.retrieval.embedding import EmbeddingModel, build_embedding
from app.retrieval.keyword import score_keyword

logger = logging.getLogger(__name__)

# 证据片段上限：超过这个长度用户不会看，卡片也放不下
QUOTE_MAX = 120
# 召回条数：够生成 2 条建议即可，多了只是把噪音塞给模型
DEFAULT_TOP_K = 8
# 余弦阈值：低于它视为"库里没有相关内容"，宁可不出建议
# 向量召回的最低相关度。
#
# **0.55 是错的，会让人以为"库里没内容"**：本机 ONNX 模型下实测
#   直接相关 0.509 / 同书相关 0.490 / 弱相关 0.356 / 无关 0.266
# 阈值 0.55 会把前两类全滤掉，于是 B 模块在真实数据上永远报"依据不足"，
# 功能实际不可用——而 Mock 用的 HashEmbedding 分数分布完全不同，测试照样全绿。
# 取 0.42：低于弱相关、高于无关，落在"相关"与"擦边"之间。
# 换嵌入模型后必须重测这个数（`scripts/smoke_modules.py` 会打印实测余弦）。
DEFAULT_MIN_RELEVANCE = 0.42
# 纯关键词召回（无向量）时的 relevance 上限：关键词命中不等于语义相关
KEYWORD_RELEVANCE_CAP = 0.55
# 关键词召回的最低门槛。作用是挡住**完全没有重叠**的条目（score=0），
# 不是做精确相关性判断——中文 2-gram 的重叠率天然偏低，
# 门槛设高了会把真实相关的条目一起滤掉（0.2 就会，已实测）。
KEYWORD_MIN_RELEVANCE = 0.05
# 建议里同书证据上限：防"一本书撑起全部建议"
MAX_PER_BOOK = 2
# 通读笔记单次装配上限：通读笔记是"背景脉络"，塞太多只会稀释用户自己的内容
MAX_AGENT_SUMMARIES = 3
# 通读笔记的相关性封顶：它代表"整本书的脉络"，与具体疑问的相关性本就该
# 低于一条直接命中的划线，所以封在关键词封顶（0.55）之下。
AGENT_RELEVANCE_CAP = 0.5
# 通读笔记召回的最低门槛：完全词面不相关的书不进池（与 keyword 路径同一口径）
AGENT_MIN_RELEVANCE = 0.05
# 书籍原文片段单次装配上限：它是「书里怎么说」的直接依据，比通读笔记更具体，
# 但一次塞太多同样会稀释模型注意力。
# 4 = 「两本书 × 每本两段」。压成本不靠这个数：真正喂给模型的是
# `QUOTE_MAX`（120 字）的引文，不是整块（1200 字），量级可忽略。
MAX_BOOK_EXCERPTS = 4
# 书籍原文片段的相关性封顶：原文是关键词检索（BM25 近似），不是向量语义召回，
# 所以封在通读笔记（0.5）与关键词封顶（0.55）之间偏上——它比摘要更具体、比手记更弱。
# **实测会真的触发**（5 本真书 / 20 组查询）：相关命中原始分 0.20–0.667，
# 其中「什么是心理表征」在 194 块里有 13 块 ≥0.55。不封顶的话，一段原句的
# 分数会盖过用户自己划线的内容，排序就反过来压住了用户输入。
BOOK_EXCERPT_RELEVANCE_CAP = 0.55
# 书籍原文片段的最低相关度。
# **实测取 0.14**（5 本真书 / 14 组查询，逐条人工核对前三条）。分数口径是
# `score_keyword(问句去掉书名, 章节标题, 正文)`：
#   无关问句的最高命中：0.109 / 0.133（拿「今天中午吃什么比较好」问一本讲通才的书）
#   真实命中的最低值：0.145（「我该不该早点确定专业方向」）、其余 0.18–0.55
# 分界就在 0.133 与 0.145 之间，**这条空档很窄**：`score_keyword` 是二字窗口命中率，
# 量化本身就粗。所以这是「实测出来的折中」，不是精确科学；换成向量召回后应重测，
# 而不是沿用此值。另注意：问句本身在书里没有对应内容时（如拿「刷手机」问一本
# 只讲时间分配的小册子，最高 0.031），返回空是**正确**的，不该为了有内容而降低门槛。
BOOK_MIN_RELEVANCE = 0.14
# 单本书最多取几个原文片段：一本几十上百块，全塞进来会变成「照着这本书念」
MAX_EXCERPTS_PER_BOOK = 2
# 跨书的相对门槛：低于「最高分 × 这个比例」的一律不要。
# 绝对门槛只能挡住「完全无关」，挡不住「随便沾到两个词」——实测「什么是心理表征」
# 在拿到 0.55 / 0.33 两条真命中之后，后面跟的是 0.30 的无关段落。
# 0.55 是实测出来的分界：保留 0.33（0.6 倍）同时滤掉 0.30（0.55 倍）。
BOOK_RELATIVE_FLOOR = 0.55
# 「介绍这本书」这类诉求的代表性段落权重。
# 它不是精确命中，只是「我挑了几段能代表全书的位置」，所以固定给中位分，
# 既不冒充高相关，也不会低于 BOOK_MIN_RELEVANCE 被自己滤掉。
BOOK_OVERVIEW_RELEVANCE = 0.3

# 「介绍这本书」这类 query 里除了书名，只剩这些没有检索价值的词。
# 这类 query **不能走相关性检索**：它跟正文没有词面关系，实测在全书同分的情况
# （书名命中 → 每块都 1.0）下，排出来的是版权页、前言、赞誉之辞。
_OVERVIEW_WORDS = (
    "介绍", "讲讲", "讲一讲", "说说", "聊聊", "看看", "讲什么", "说什么", "写了什么",
    "讲啥", "主要内容", "大概", "概述", "概览", "简介", "总结", "这本书", "这本",
    "本书", "怎么样", "好看吗", "值不值得", "是什么书", "讲的是什么", "主要讲",
)
# 版权页 / 丛书序这类块没有讨论价值，任何路径都不该把它当依据，分两档：
# - 强标记：正常正文里基本不会出现，命中一个就丢；
# - 弱标记：正文里出现一次「定价」「印刷」很正常，命中 ≥2 个才判定为出版信息。
_FRONT_MATTER_STRONG = (
    "图书在版编目", "CIP", "责任编辑", "版权所有", "ISBN",
    "关于本丛书", "出版说明", "责任印制", "封面设计", "出品人",
)
_FRONT_MATTER_WEAK = ("书号", "出版发行", "印张", "开本", "印 数", "字数", "定价")

BASIS_LIBRARY = "library"
BASIS_AGENT = "agent_summary"
BASIS_BOOK = "book"  # 智能体通读书籍的原文片段（用户没划线/手记时仍可据此出建议）
BASIS_EXTERNAL = "external"  # 仅占位：本模块不接受，用于让调用方显式区分

LEVEL_L1 = "l1"
LEVEL_L2 = "l2"
LEVEL_L3 = "l3"

# 可按 item_id 跳回条目详情的来源（manual 没有原文定位，但条目本身可打开）
_ITEM_JUMPABLE_SOURCES = frozenset({"manual"})


@dataclass
class Evidence:
    """一条可用依据。

    `as_card()` 是卡片序列化的唯一出口——卡片的证据字段长什么样，只由这里决定。
    """

    evidence_id: str
    kind: str  # knowledge_item | agent_summary
    basis: str  # library | agent_summary
    source: str  # book / weread / manual / agent
    title: str
    quote: str
    locator: dict = field(default_factory=dict)
    retrieval: str = "vector"
    relevance: float = 0.0
    item_id: str = ""
    level: str = LEVEL_L3

    @property
    def can_cite(self) -> bool:
        """能否被 `basis_item_ids` 引用：必须可溯源（l3）。"""
        return self.level == LEVEL_L3

    @property
    def jumpable(self) -> bool:
        return _jumpable(self.source, self.locator, self.item_id)

    def as_card(self) -> dict:
        """卡片载荷。字段名与 B 模块 4.3 一致，前端直接渲染。

        `item_id` 必须带出去：没有它，`manual` 来源的证据在卡片上点不开
        （条文只有 locator，manual 的 locator 是空的）。
        """
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "basis": self.basis,
            "source": self.source,
            "title": self.title,
            "quote": self.quote,
            "locator": dict(self.locator or {}),
            "retrieval": self.retrieval,
            "relevance": round(float(self.relevance), 4),
            "jumpable": self.jumpable,
            "level": self.level,
            "item_id": self.item_id,
        }


# ---- 片段抽取 ---------------------------------------------------------------


def extract_quote(content: str, query: str = "", *, max_len: int = QUOTE_MAX) -> str:
    """从原文里抽一段能代表内容的片段。

    返回值**必须**是 `content` 的子串（调用方据此校验），所以这里只做「选窗口 + 截断」，
    绝不做改写、补全或摘要——一旦改写，`quote in raw_content` 就断了，溯源也就断了。

    选窗口策略：按句切分，取与 query 二元组重叠最多的那句；都不重叠时取开头。
    中文没有空格分词，用 2-gram 集合重叠是最稳的近似。
    """
    text = (content or "").strip()
    if not text:
        return ""
    if len(text) <= max_len:
        return text

    sentences = _split_sentences(text)
    if query:
        q_grams = _grams(query, 2)
        if q_grams:
            best, best_score = "", -1.0
            for sent in sentences:
                if not sent.strip():
                    continue
                overlap = len(q_grams & _grams(sent, 2))
                if overlap > best_score:
                    best, best_score = sent, overlap
            if best_score > 0 and best.strip():
                return _clip(best.strip(), max_len)

    return _clip(text, max_len)


def _split_sentences(text: str) -> list[str]:
    """按中英文句末标点与换行切句，保留标点。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in "。！？!?\n":
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


def _clip(text: str, max_len: int) -> str:
    """截到 max_len 以内，优先在标点处断开，避免半句话。"""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for ch in "。！？!?，,；;":
        pos = cut.rfind(ch)
        # 断点太靠前就不用它（否则会切出极短片段）
        if pos >= max_len // 2:
            return cut[: pos + 1]
    return cut


def _grams(text: str, n: int) -> set[str]:
    clean = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def verify_quote(content: str, quote: str) -> bool:
    """quote 是否真的出自这段原文（溯源校验的硬条件）。"""
    return bool(quote) and bool(content) and quote.strip() in content


def _jumpable(source: str, locator: dict, item_id: str) -> bool:
    """有没有"点得回去"的定位。

    - book：需要字符区间（阅读器可高亮）；没有区间仍可退到条目详情
    - weread：需要 deepLink 或章节信息
    - manual：只需 item_id（打开条目本身）
    """
    loc = locator or {}
    if source == "book":
        if loc.get("char_start") is not None and loc.get("char_end") is not None:
            return True
        return bool(item_id)
    if source == "weread":
        return bool(loc.get("deep_link") or loc.get("chapter_title") or item_id)
    if source == "agent":
        # 通读笔记没有具体位置，能点回"这本书"即可
        return bool(loc.get("book_id"))
    if source in _ITEM_JUMPABLE_SOURCES:
        return bool(item_id)
    return bool(item_id)


# ---- 召回与判定 -------------------------------------------------------------


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _locator_of(item: KnowledgeItem) -> dict:
    loc = dict(item.source_locator or {})
    # 内置书的 locator 只有章节与字符区间，没有 book_id（book_id 存在 source_item_id 里）。
    # 前端要用它拼 `reader.html?book=…&char=…`，这里补上——两处各取一次迟早会忘。
    if (item.source or "") == "book" and item.source_item_id and "book_id" not in loc:
        loc["book_id"] = str(item.source_item_id)
    return loc


def _evidence_id_of(item: KnowledgeItem) -> str:
    return f"ki:{item.id}"


def build_evidence(
    session: Session,
    *,
    user_id: str,
    query: str,
    top_k: int = DEFAULT_TOP_K,
    min_relevance: float = DEFAULT_MIN_RELEVANCE,
    retriever=None,
    embedding: EmbeddingModel | None = None,
) -> list[Evidence]:
    """按 query 召回可用依据（只含 `knowledge_item`，通读笔记由调用方另行装配）。

    参数可注入（`retriever` / `embedding`）是为了测试不加载真模型；生产走默认惰性构建。
    """
    q = (query or "").strip()
    if not q:
        return []

    emb = embedding
    if emb is None:
        try:
            emb = build_embedding()
        except Exception as exc:  # noqa: BLE001 - 模型不可用不该让建议链路直接崩
            logger.warning("grounding: embedding 不可用，仅关键词召回: %s", exc)
            emb = None

    if retriever is None:
        try:
            from app.retrieval.retriever import Retriever

            retriever = Retriever(emb) if emb is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("grounding: retriever 构建失败: %s", exc)
            retriever = None

    hits = []
    if retriever is not None:
        try:
            hits = retriever.retrieve(
                q, user_id=user_id, top_k=top_k, vec_min_score=min_relevance
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("grounding: 召回失败，按无依据处理: %s", exc)
            hits = []
    if not hits:
        return []

    ids = [h.item_id for h in hits]
    stmt = select(KnowledgeItem).where(
        KnowledgeItem.id.in_(ids),
        KnowledgeItem.user_id == user_id,
        KnowledgeItem.is_deleted.is_(False),
    )
    items = {i.id: i for i in session.scalars(stmt)}

    # query 向量只算一次：候选的 relevance 全靠它重算真实余弦
    query_vec: list[float] | None = None
    if emb is not None:
        try:
            query_vec = emb.embed([q])[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("grounding: query 向量化失败，relevance 降级为关键词分: %s", exc)
            query_vec = None

    out: list[Evidence] = []
    for hit in hits:
        item = items.get(hit.item_id)
        if item is None:  # 软删 / 越权 / 已被清理
            continue

        quote = extract_quote(item.raw_content or "", q)
        if not quote:
            continue  # 空正文：连 l2 都算不上，直接丢

        vector_hit = "vector" in hit.channels
        if query_vec is not None and item.embedding:
            try:
                relevance = cosine(query_vec, EmbeddingModel.loads(item.embedding))
            except Exception:  # noqa: BLE001 - 坏向量不影响降级
                relevance = 0.0
        else:
            relevance = 0.0
            vector_hit = False

        if item.embed_status == "embedded" and vector_hit and relevance > 0.0:
            retrieval = "vector"
        else:
            # 降级路径：关键词分当 relevance，但封顶（关键词命中 ≠ 语义相关）
            retrieval = "keyword"
            kw = score_keyword(q, item.title or "", item.raw_content or "")
            relevance = min(KEYWORD_RELEVANCE_CAP, max(relevance, kw))

        if relevance < min_relevance and retrieval == "vector":
            continue  # 余弦不过阈值：库里确实没有相关内容
        if relevance < KEYWORD_MIN_RELEVANCE and retrieval == "keyword":
            continue  # 关键词只擦到边也算依据的话，证据池就成了噪音池

        locator = _locator_of(item)
        jumpable = _jumpable(item.source or "manual", locator, item.id)
        # 能引用 = 片段真出自原文（已由 extract_quote 保证）且点得回去
        level = LEVEL_L3 if jumpable else LEVEL_L2

        out.append(
            Evidence(
                evidence_id=_evidence_id_of(item),
                kind="knowledge_item",
                basis=BASIS_LIBRARY,
                source=item.source or "manual",
                title=item.title or "",
                quote=quote,
                locator=locator,
                retrieval=retrieval,
                relevance=float(relevance),
                item_id=item.id,
                level=level,
            )
        )

    # 可引用优先，其次相关性；同分保持原召回顺序（sort 稳定）
    out.sort(key=lambda e: (not e.can_cite, -e.relevance))
    return out


def dedupe_by_book(evidence: list[Evidence], *, max_per_book: int = MAX_PER_BOOK) -> list[Evidence]:
    """同书证据限流（防"一本书撑起全部建议"）。

    只对**有书归属**的来源限流（`book` / `weread`）。手记（`manual`）没有"书"的概念，
    强行按标题归并会把两条标题相同的独立笔记（都叫「读书笔记」很常见）误裁掉——
    那是真的少给了用户依据，比多给一条更难发现。
    """
    counts: dict[str, int] = {}
    kept: list[Evidence] = []
    for ev in evidence:
        if ev.source not in ("book", "weread"):
            kept.append(ev)
            continue
        key = str((ev.locator or {}).get("book_id") or ev.title or ev.evidence_id)
        if counts.get(key, 0) >= max_per_book:
            continue
        counts[key] = counts.get(key, 0) + 1
        kept.append(ev)
    return kept


def build_agent_summaries(
    session: Session,
    *,
    user_id: str,
    query: str,
    limit: int = MAX_AGENT_SUMMARIES,
    min_relevance: float = AGENT_MIN_RELEVANCE,
) -> list[Evidence]:
    """把已通读的书装配成 `agent_summary` 依据。

    与 `build_evidence` 的分工：那里只产出用户同步的内容（划线 / 手记），
    这里产出**智能体通读整本书后写的总评**——它没有向量，相关性用关键词分近似，
    并封顶在 `AGENT_RELEVANCE_CAP`（通读笔记是背景脉络，不该显得和一条直接命中的
    划线一样相关）。

    `basis=agent_summary` 本身不是豁免：`verify_citations` 已要求每条建议至少
    1 条 `library`，所以通读笔记**天然不得单独构成依据**，这里不需要再写一遍。

    读失败 / 空库一律返回 `[]`，不让这条支线把建议链路带崩。
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        from app.domain.models.book import Book
        from app.domain.models.book_agent_reading import BookAgentReading

        rows = session.execute(
            select(Book.title, Book.id, BookAgentReading.summary)
            .join(BookAgentReading, BookAgentReading.book_id == Book.id)
            .where(
                BookAgentReading.user_id == user_id,
                BookAgentReading.status == "done",
                BookAgentReading.is_deleted.is_(False),
            )
        ).all()
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("grounding: 读取通读笔记失败，按无处理: %s", exc)
        return []

    out: list[Evidence] = []
    for title, book_id, summary in rows:
        text = (summary or "").strip()
        if not text:
            continue
        relevance = score_keyword(q, title or "", text)
        if relevance < min_relevance:
            continue
        quote = _clip(text, QUOTE_MAX)
        if not quote:
            continue
        out.append(
            Evidence(
                evidence_id=f"agent:{book_id}",
                kind="agent_summary",
                basis=BASIS_AGENT,
                source="agent",
                title=f"《{title}》通读笔记" if title else "通读笔记",
                quote=quote,
                locator={"book_id": str(book_id)},
                retrieval="agent",
                relevance=min(AGENT_RELEVANCE_CAP, relevance),
                item_id="",
                level=LEVEL_L3,
            )
        )

    # 只有几本通读，相关性排序后取前 limit 本即可
    out.sort(key=lambda e: -e.relevance)
    return out[:limit]


def _short_book_title(title: str) -> str:
    """书名主标题：去掉副标题 / 推广语（与 suggestion 侧同一口径）。

    真实书名常见「时间管理（击退拖延症、处理多重任务……）」这种形态，
    整串参与比对只会让括号里的营销词冒充「书名」。
    """
    body = re.split(r"[（(:：]", (title or "").strip(), maxsplit=1)[0].strip()
    return body or (title or "").strip()


def is_overview_query(query: str, book_title: str = "") -> bool:
    """这句诉求是不是「概括讲讲这本书」，而不是问某个具体内容。

    判据：把书名和 `_OVERVIEW_WORDS` 里的泛词全部剥掉，剩下没有实义内容。
    例：「介绍这本书」「讲讲《刻意练习》」→ True；
        「刻意练习和普通练习有什么区别」→ False（剩下「和普通有什么区别」）。
    """
    text = (query or "").strip()
    if not text:
        return False
    title = _short_book_title(book_title)
    if title:
        text = text.replace(title, " ")
    # 长词优先替换，避免「主要内容」被「内容」这类短词切碎后残留
    for word in sorted(_OVERVIEW_WORDS, key=len, reverse=True):
        text = text.replace(word, " ")
    text = re.sub(r"[\s，。、？！：；·\-—…,.?!:;\"'“”‘’（）()《》\[\]]+", "", text)
    return not text


def _looks_like_front_matter(text: str) -> bool:
    """版权页 / 扉页 / 出版信息——块里这类标记密集就判定为无讨论价值。"""
    head = (text or "")[:200]
    if any(marker in head for marker in _FRONT_MATTER_STRONG):
        return True
    return sum(1 for marker in _FRONT_MATTER_WEAK if marker in head) >= 2


def _mentions_title(query: str, title: str) -> bool:
    """这句话是不是明确点了这本书（用主标题比对，太短的标题不算）。"""
    short = _short_book_title(title)
    return len(short) >= 2 and short in (query or "")


def _strip_title(query: str, title: str) -> str:
    """把书名从 query 里去掉——书名不是「问题」，算进相关性只会把全书抬平。"""
    short = _short_book_title(title)
    if len(short) >= 2 and short in (query or ""):
        return (query or "").replace(short, " ").strip()
    return (query or "").strip()


def _book_chunks(session: Session, book) -> list:
    """按 seq 取这本书已落库的块（切块规则稳定，坐标可复现）。"""
    from app.domain.models.book_chunk import BookChunk

    return list(
        session.scalars(
            select(BookChunk)
            .where(BookChunk.book_id == book.id, BookChunk.user_id == book.user_id)
            .order_by(BookChunk.seq)
        )
    )


def _spread_chunks(session: Session, book, *, limit: int) -> list:
    """在全书里等距取样，供「介绍这本书」用。

    为什么要等距而不是取最相关：书名型 query 对全书每块都同分（书名命中 → 1.0），
    取「最相关」等于取「最前面」，而最前面往往是版权页和前言。
    概括一本书需要的是覆盖面——开头、中段、结尾各取一段，比开头取两段有用得多。
    """
    rows = _book_chunks(session, book)
    usable = [
        row for row in rows
        if len((row.text or "").strip()) >= 200 and not _looks_like_front_matter(row.text)
    ]
    if not usable:  # 短书 / 全是短块时不挑，宁可给几段短的也别给空
        usable = rows
    if not usable:
        return []
    if len(usable) <= limit:
        return usable
    # 每段取自身区间的中点，避免正好落在章节交界的空行上
    step = len(usable) / limit
    picked = []
    for index in range(limit):
        at = min(len(usable) - 1, int((index + 0.5) * step))
        if usable[at] not in picked:
            picked.append(usable[at])
    return picked


def build_book_excerpts(
    session: Session,
    *,
    user_id: str,
    query: str,
    limit: int = MAX_BOOK_EXCERPTS,
    top_k_per_book: int = MAX_EXCERPTS_PER_BOOK,
    min_relevance: float = BOOK_MIN_RELEVANCE,
) -> list[Evidence]:
    """把智能体已通读书里的原文片段装配成 `book` 依据。

    这是「建议不只来自用户笔记」的落点：用户没划线 / 没手记时，仍能从智能体读过的
    书里检索出原句作为依据——「介绍这本书」「这本书里怎么说的」这类诉求，不该因为
    用户笔记为空就一句「查不到」。与 `build_agent_summaries` 的分工：那个是整本书的
    总评脉络，这里是**具体到某一段的原句**（`basis=book`，可独立构成依据）。

    两条取法，判据是「这句诉求有没有具体内容」：

    - 有具体问题 → 全书逐块打分，取最相关的几块。打分口径 = 去掉书名后的问句
      与「章节标题 + 正文」的二字窗口重叠（`score_keyword`），跨书同一尺度可比。
      书名既不参与检索也不参与打分：它是「这本书」的标记，不是问题的一部分，
      带上它会让整本书的每一块同分（实测连无关问句都能让全部块过线）。
    - 只问「介绍这本书」→ 全书等距取样（见 `_spread_chunks`）。相关性检索在这类
      query 上会退化成「取最前面」，实测返回的是版权页。

    另外两条硬规则：query 点名的书没通读过就直接返回空（不拿别的书顶替）；
    版权页 / 丛书序这类块永远不当依据。

    读失败 / 空库返回 `[]`，不让这条支线把建议链路带崩。
    """
    q = (query or "").strip()
    if not q:
        return []
    try:
        from app.books import chunking
        from app.domain.models.book import Book
        from app.domain.models.book_agent_reading import BookAgentReading

        book_ids = session.execute(
            select(BookAgentReading.book_id).where(
                BookAgentReading.user_id == user_id,
                BookAgentReading.status == "done",
                BookAgentReading.is_deleted.is_(False),
            )
        ).scalars().all()
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("grounding: 读取已通读书籍失败，按无处理: %s", exc)
        return []
    if not book_ids:
        return []

    try:
        # 取**全部**书（含没通读的）：要判断「用户问的这本我是不是还没读」——
        # 没读就该老实说没有，而不是拿别的书里「碰巧提到这个词」的段落顶上。
        books = session.scalars(
            select(Book).where(Book.user_id == user_id, Book.is_deleted.is_(False))
        ).all()
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("grounding: 读取书籍失败，按无处理: %s", exc)
        return []

    read_ids = set(book_ids)
    mentioned = [book for book in books if _mentions_title(q, book.title)]
    if mentioned:
        # 点名的书没通读 → 直接返回空：拿别的书凑依据是「有出处但出处不对」，
        # 比一句「这本我还没读」更糟。
        targets = [book for book in mentioned if book.id in read_ids]
        if not targets:
            logger.info("grounding: 点名的书尚未通读，不取原文依据")
            return []
    else:
        targets = [book for book in books if book.id in read_ids]

    out: list[Evidence] = []
    # 「介绍这本书」是**对着某一本**说的：只要某本书的书名能对上，就只取这本书，
    # 不再去别的书里捞「碰巧提到这两个词」的段落——实测问《刻意练习》时，
    # 另一本书里谈「刻意练习」的段落会以更高分挤掉这本自己的内容。
    overview_books = [book for book in targets if is_overview_query(q, book.title)]
    if overview_books:
        targets = overview_books

    for book in targets:
        try:
            chunking.ensure_chunks(session, book)
        except Exception as exc:  # pragma: no cover - 单本书失败不阻断其余
            logger.warning("grounding: 建块失败 book=%s err=%s", book.id, exc)
            continue

        if overview_books:
            for row in _spread_chunks(session, book, limit=top_k_per_book):
                if _looks_like_front_matter(row.text):
                    continue
                out.append(
                    _book_evidence(
                        book,
                        {
                            "text": row.text or "",
                            "chapter_title": row.chapter_title or "",
                            "char_start": row.char_start,
                            "char_end": row.char_end,
                        },
                        relevance=BOOK_OVERVIEW_RELEVANCE,
                        retrieval="book_overview",
                    )
                )
            continue

        # 检索词只用「问题」本身：书名在这一步纯属噪音——实测带书名问
        # 「什么是儿童自我状态」，BM25 的头两条是版权页和一段无关对话
        # （版权页里书名出现了好几次）。范围本来就已经限定在这一本书内。
        question = _strip_title(q, book.title) or q
        # **全量打分而不是先让书内 BM25 选两条再打分**：两个打分器口径不同
        # （BM25 带 IDF 与长度归一，关键词算覆盖率），BM25 选出来的两条常常
        # 不是关键词口径下最相关的两条，实测「通才和专才哪个更好」因此直接变成 0 条。
        # 代价是每次扫一本书的块（实测 200 块约 20ms），换来的是「选出来的就是报出的分」。
        scored = []
        for row in _book_chunks(session, book):
            text = (row.text or "").strip()
            if not text or _looks_like_front_matter(text):
                continue
            relevance = score_keyword(question, row.chapter_title or "", text)
            if relevance < min_relevance:
                continue
            scored.append((relevance, row))
        scored.sort(key=lambda pair: -pair[0])
        for relevance, row in scored[:top_k_per_book]:
            out.append(
                _book_evidence(
                    book,
                    {
                        "text": row.text or "",
                        "chapter_title": row.chapter_title or "",
                        "char_start": row.char_start,
                        "char_end": row.char_end,
                    },
                    relevance=relevance,
                    retrieval="book",
                )
            )

    if not out:
        return []
    # 相对门槛：绝对门槛拦不住「沾到两个词」的段落，靠与最高分的比值再切一刀。
    # 只对内容型生效——概览型本来就是代表性取样，不该被最高分把自己裁掉。
    if not overview_books:
        floor = max(ev.relevance for ev in out) * BOOK_RELATIVE_FLOOR
        out = [ev for ev in out if ev.relevance >= floor]
    out.sort(key=lambda e: -e.relevance)
    return out[:limit]


def _book_evidence(book, hit: dict, *, relevance: float, retrieval: str) -> Evidence:
    """把一条书内片段落成 `book` 依据。两条取法共用这一处，字段不会分叉。"""
    chapter_title = str(hit.get("chapter_title") or "")
    text = (hit.get("text") or "").strip()
    char_start = int(hit.get("char_start") or 0)
    return Evidence(
        evidence_id=f"book:{book.id}:{char_start}",
        kind="book_excerpt",
        basis=BASIS_BOOK,
        source="book",
        title=f"《{book.title}》" + (f"·{chapter_title}" if chapter_title else ""),
        quote=_clip(text, QUOTE_MAX),
        locator={
            "book_id": str(book.id),
            "char_start": char_start,
            "char_end": int(hit.get("char_end") or 0),
            "chapter_title": chapter_title,
        },
        retrieval=retrieval,
        relevance=min(BOOK_EXCERPT_RELEVANCE_CAP, relevance),
        item_id="",
        level=LEVEL_L3,
    )


# ---- 引用校验 ---------------------------------------------------------------


def verify_citations(items: list[dict], pool: list[Evidence]) -> tuple[list[dict], int]:
    """校验模型给出的 `basis_item_ids`，剔除引用幻觉。

    规则（任一不满足 → 整条丢弃）：
    1. `basis_item_ids` 非空；
    2. 每个 id 都在本次证据池内；
    3. 被引用的证据 `can_cite`（l3）；
    4. 每条建议至少 1 条 `library` 或 `book` 级依据——通读笔记（`agent_summary`）
       不得单独构成依据；但智能体通读过的书原文（`book`）可以，因为它和用户摘录
       一样可溯源、可跳回原句，不是「读了书写的总评」那种二手内容。

    为什么整条丢弃而不是删掉 id 保留文案：文案本身就是由那些"不存在的依据"支撑的，
    留下文案等于留下幻觉，只是把出处抹掉了。
    """
    by_id = {ev.evidence_id: ev for ev in pool}
    kept: list[dict] = []
    dropped = 0

    for item in items or []:
        ids = [str(x) for x in (item.get("basis_item_ids") or []) if str(x).strip()]
        if not ids:
            dropped += 1
            continue
        refs = [by_id.get(i) for i in ids]
        if any(r is None for r in refs) or any(not r.can_cite for r in refs):  # type: ignore[union-attr]
            dropped += 1
            continue
        if not any(r.basis in (BASIS_LIBRARY, BASIS_BOOK) for r in refs):  # type: ignore[union-attr]
            dropped += 1
            continue
        kept.append(item)

    return kept, dropped


def confidence_for(
    *,
    basis_count: int,
    mean_relevance: float,
    slots_complete: bool,
    dropped: int,
) -> float:
    """建议置信度（代码算，不取模型自评）。

    与 `docs/验收自检与修正-2026-10-20.md` 修正 4 的公式一致：
    `0.4*basis + 0.3*relevance + 0.2*槽位 + 0.1*无丢弃`。
    槽位不齐时整体上限 0.6——没确认目标的建议，再自洽也不该显得很可靠。
    """
    basis = 0.4 * min(1.0, max(0, basis_count) / 3.0)
    rel = 0.3 * min(1.0, max(0.0, mean_relevance))
    slots = 0.2 * (1.0 if slots_complete else 0.3)
    clean = 0.1 * (1.0 if dropped == 0 else 0.0)
    value = basis + rel + slots + clean
    if not slots_complete:
        value = min(value, 0.6)
    return round(min(1.0, max(0.0, value)), 3)


def library_share(pool: list[Evidence]) -> float:
    """证据池里 library 级占比（调试与埋点用）。"""
    if not pool:
        return 0.0
    return sum(1 for e in pool if e.basis == BASIS_LIBRARY) / len(pool)
