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

BASIS_LIBRARY = "library"
BASIS_AGENT = "agent_summary"
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


# ---- 引用校验 ---------------------------------------------------------------


def verify_citations(items: list[dict], pool: list[Evidence]) -> tuple[list[dict], int]:
    """校验模型给出的 `basis_item_ids`，剔除引用幻觉。

    规则（任一不满足 → 整条丢弃）：
    1. `basis_item_ids` 非空；
    2. 每个 id 都在本次证据池内；
    3. 被引用的证据 `can_cite`（l3）；
    4. 每条建议至少 1 条 `library` 级依据——通读笔记不得单独构成依据。

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
        if not any(r.basis == BASIS_LIBRARY for r in refs):  # type: ignore[union-attr]
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
