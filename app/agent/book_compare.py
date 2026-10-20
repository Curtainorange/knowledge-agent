"""书籍对比（C 模块）：先对齐主题，再让模型说差异。

## 为什么必须先生对齐

把两本书的分章要点整段丢给模型"找对应"，它会硬凑：两本书明明没有共同章节，
它也能给你编出三组对比。**对比的前提是"这两章确实在讲同一件事"**，
这件事可以用字面相似度算出来，不该由模型猜。

所以流程是：

1. 代码按标题/要点算相似度，贪心配对（每章只用一次）→ 得到对齐的主题对；
2. 没配上的章节单独列出（"只有 A 涉及"）；
3. 模型**只对给定主题对**判断关系（一致 / 互补 / 冲突 / 各有侧重）。

## 为什么结果要缓存

一次对比要读两本书的要点 + 一次 reasoning 调用。同一对书反复问是常见行为，
所以按规范化书对（先 A 后 B 与先 B 后 A 是同一对）落库，重复提问直接取缓存。

## 为什么只支持内置书

微信读书同步过来的只有笔记和摘录，**没有全文**，也就没有章节结构——
那种情况下"对齐"是无从谈起的，硬做只能拿两本笔记的标题对标题，
产出的对比会非常空。所以这里明确要求：两本书都要有智能体通读记录。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.models.book_comparison import BookComparison
from app.domain.repositories.book_agent_reading_repository import BookAgentReadingRepository
from app.llm.gateway import ModelGateway
from app.llm.prompts import BOOK_COMPARE
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

RELATIONS = ("一致", "互补", "冲突", "各有侧重", "一方独有")
# 对齐阈值：低于它就不算"讲同一件事"。0.25 是实测下来比较保守的一个值——
# 中文两字词的 Jaccard 本来就低，设高了会把真正相关的章节对漏掉。
ALIGN_THRESHOLD = 0.25
# 每本书最多参与对齐的章节数：长书的章节要点很长，全塞进去会挤爆上下文
MAX_CHAPTERS = 12
# 最多几组主题对：再多用户也看不完
MAX_PAIRS = 8
# 单个章节要点送进模型时的截断长度
CHAPTER_CHARS = 400


class CompareRow(BaseModel):
    topic: str = Field(default="", max_length=120)
    relation: str = Field(default="")
    a_says: str = Field(default="", max_length=400)
    b_says: str = Field(default="", max_length=400)
    difference: str = Field(default="", max_length=400)
    a_index: int = -1
    b_index: int = -1


class CompareDraft(BaseModel):
    overview: str = Field(default="", max_length=600)
    rows: list[CompareRow] = Field(default_factory=list)


@dataclass
class CompareOutcome:
    state: str  # ok | cached | not_read | no_overlap | degraded | not_found
    book_a: dict = field(default_factory=dict)
    book_b: dict = field(default_factory=dict)
    overview: str = ""
    rows: list = field(default_factory=list)
    aligned: list = field(default_factory=list)
    unmatched_a: list = field(default_factory=list)
    unmatched_b: list = field(default_factory=list)
    note: str = ""


# ---- 对齐（纯函数）----------------------------------------------------------


def _grams(text: str, n: int = 2) -> set[str]:
    clean = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def topic_similarity(a: str, b: str) -> float:
    """两段主题文字的字面相似度（Jaccard）。

    为什么用字面而不是向量：章节标题很短（"索引与查询优化"），
    短文本的向量很糊；字面重合在这种长度上反而更可靠，而且**可解释**——
    能直接告诉用户"因为这两章标题都含『索引』"。
    """
    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _chapter_topic(chapter: dict) -> str:
    title = str(chapter.get("title") or "").strip()
    if title:
        return title
    return str(chapter.get("gist") or "").strip()[:40]


def _chapter_gist(chapter: dict) -> str:
    parts: list[str] = []
    gist = str(chapter.get("gist") or "").strip()
    if gist:
        parts.append(gist)
    for point in (chapter.get("points") or [])[:3]:
        text = str(point).strip()
        if text:
            parts.append("· " + text)
    return "\n".join(parts)[:CHAPTER_CHARS]


def _normalize_chapters(chapters: list | None) -> list[dict]:
    """统一成 [{index, pos, title, topic, gist}]，按 index 去重（重复章节只留第一个）。"""
    out: list[dict] = []
    seen: set[int] = set()
    for pos, chapter in enumerate(chapters or []):
        if not isinstance(chapter, dict):
            continue
        raw_index = chapter.get("index")
        index = int(raw_index) if isinstance(raw_index, (int, float)) else pos + 1
        if index in seen:
            continue
        seen.add(index)
        topic = _chapter_topic(chapter)
        if not topic:
            continue
        out.append({
            "index": index,
            "pos": pos,
            "title": str(chapter.get("title") or topic)[:80],
            "topic": topic,
            "gist": _chapter_gist(chapter),
        })
        if len(out) >= MAX_CHAPTERS:
            break
    return out


def align(
    chapters_a: list[dict],
    chapters_b: list[dict],
    *,
    threshold: float = ALIGN_THRESHOLD,
    max_pairs: int = MAX_PAIRS,
) -> tuple[list[dict], list[dict], list[dict]]:
    """贪心对齐：分数高的先配，每章只用一次。

    返回 (对齐对, 仅 A, 仅 B)。**仅一侧的部分也要返回**——
    "这本书讲了、那本没讲"本身就是有价值的对比结论，
    而且它让用户知道这次比较的边界在哪。
    """
    scored: list[tuple[float, dict, dict]] = []
    for ca in chapters_a:
        for cb in chapters_b:
            score = topic_similarity(ca["topic"], cb["topic"])
            if score >= threshold:
                scored.append((score, ca, cb))
    scored.sort(key=lambda item: -item[0])

    used_a: set[int] = set()
    used_b: set[int] = set()
    aligned: list[dict] = []
    for score, ca, cb in scored:
        if ca["index"] in used_a or cb["index"] in used_b:
            continue
        used_a.add(ca["index"])
        used_b.add(cb["index"])
        aligned.append({
            "topic": ca["topic"],
            "score": round(score, 3),
            "a_index": ca["index"],
            "b_index": cb["index"],
            "a_title": ca["title"],
            "b_title": cb["title"],
        })
        if len(aligned) >= max_pairs:
            break

    unmatched_a = [
        {"index": c["index"], "title": c["title"], "gist": c["gist"]}
        for c in chapters_a if c["index"] not in used_a
    ]
    unmatched_b = [
        {"index": c["index"], "title": c["title"], "gist": c["gist"]}
        for c in chapters_b if c["index"] not in used_b
    ]
    return aligned, unmatched_a, unmatched_b


def build_payload(
    *, title_a: str, title_b: str,
    pairs: list[dict], chapters_a: list[dict], chapters_b: list[dict],
    unmatched_a: list[dict], unmatched_b: list[dict],
) -> str:
    """拼给模型的对比材料。只给对齐过的主题对 + 单边主题，不整本丢进去。"""
    by_index_a = {c["index"]: c for c in chapters_a}
    by_index_b = {c["index"]: c for c in chapters_b}
    lines: list[str] = [f"A：《{title_a}》", f"B：《{title_b}》", ""]

    for order, pair in enumerate(pairs, 1):
        ca = by_index_a.get(pair["a_index"], {})
        cb = by_index_b.get(pair["b_index"], {})
        lines.append(f"【主题对 {order}】主题：{pair['topic']}（相似度 {pair['score']}）")
        lines.append(
            f"A 第 {pair['a_index']} 章「{pair['a_title']}」：{ca.get('gist') or '（无要点）'}"
        )
        lines.append(
            f"B 第 {pair['b_index']} 章「{pair['b_title']}」：{cb.get('gist') or '（无要点）'}"
        )
        lines.append("")

    if unmatched_a:
        lines.append("【只有 A 涉及的主题】")
        for item in unmatched_a[:5]:
            lines.append(f"A 第 {item['index']} 章「{item['title']}」：{item['gist'] or '（无要点）'}")
        lines.append("")
    if unmatched_b:
        lines.append("【只有 B 涉及的主题】")
        for item in unmatched_b[:5]:
            lines.append(f"B 第 {item['index']} 章「{item['title']}」：{item['gist'] or '（无要点）'}")
        lines.append("")
    return "\n".join(lines)


# ---- 书名解析 ---------------------------------------------------------------


_TITLE_RE = re.compile(r"《([^》]{1,60})》")
_SPLIT_RE = re.compile(r"\s*(?:和|与|跟|vs\.?|VS\.?|对比|比较)\s*")


def parse_titles(text: str) -> tuple[str, str]:
    """从一句话里抽两个书名。抽不到返回空串，由调用方决定兜底策略。

    支持两种说法：`《A》和《B》`（最可靠）、`对比 A 和 B`（去掉动词后切分）。
    """
    titles = _TITLE_RE.findall(text or "")
    if len(titles) >= 2:
        return titles[0].strip(), titles[1].strip()

    body = (text or "").strip()
    for marker in ("对比一下", "对比", "比较一下", "比较", "比一比"):
        if body.startswith(marker):
            body = body[len(marker):].strip()
            break
    body = body.rstrip("？?。.！!")
    parts = [p.strip(" 的《》）)（(") for p in _SPLIT_RE.split(body) if p.strip()]
    if len(parts) >= 2:
        return parts[0][:60], parts[1][:60]
    return "", ""


# ---- 主流程 -----------------------------------------------------------------


def pair_key_of(book_a_id: str, book_b_id: str) -> str:
    """规范化书对：先 A 后 B 与先 B 后 A 是同一对，命中同一份缓存。"""
    left, right = sorted([str(book_a_id), str(book_b_id)])
    return f"{left}|{right}"


def _load_book(session: Session, *, user_id: str, book_id: str) -> Book | None:
    book = session.get(Book, book_id)
    if book is None or book.user_id != user_id or book.is_deleted:
        return None
    return book


def _book_brief(book: Book) -> dict:
    return {"book_id": book.id, "title": book.title, "author": book.author or ""}


def _cached(session: Session, *, user_id: str, pair_key: str) -> BookComparison | None:
    return session.scalars(
        select(BookComparison).where(
            BookComparison.user_id == user_id,
            BookComparison.pair_key == pair_key,
        ).limit(1)
    ).first()


def compare(
    session: Session,
    *,
    user_id: str,
    book_a_id: str = "",
    book_b_id: str = "",
    gateway: ModelGateway | None = None,
    force: bool = False,
    context: dict | None = None,
) -> CompareOutcome:
    """对比两本书。`context` 供测试注入草稿；生产不传，走网关。"""
    title_a = title_b = ""
    injected = (context or {}).get("draft")
    if injected is not None:
        title_a = str((context or {}).get("title_a") or "A 书")
        title_b = str((context or {}).get("title_b") or "B 书")

    book_a = _load_book(session, user_id=user_id, book_id=book_a_id) if book_a_id else None
    book_b = _load_book(session, user_id=user_id, book_id=book_b_id) if book_b_id else None
    if book_a_id and book_a is None:
        return CompareOutcome(state="not_found", note="这本（A）找不到了")
    if book_b_id and book_b is None:
        return CompareOutcome(state="not_found", note="这本（B）找不到了")
    if book_a is None or book_b is None:
        return CompareOutcome(state="not_found", note="要说清是哪两本书")
    if book_a.id == book_b.id:
        return CompareOutcome(state="not_found", note="两本书不能是同一本")

    title_a, title_b = book_a.title, book_b.title
    brief_a, brief_b = _book_brief(book_a), _book_brief(book_b)

    readings = BookAgentReadingRepository(session, user_id=user_id)
    reading_a = readings.get_by_book(book_a.id)
    reading_b = readings.get_by_book(book_b.id)
    if reading_a is None or reading_b is None or not reading_a.chapters_note or not reading_b.chapters_note:
        missing = []
        if reading_a is None or not reading_a.chapters_note:
            missing.append(f"《{title_a}》")
        if reading_b is None or not reading_b.chapters_note:
            missing.append(f"《{title_b}》")
        return CompareOutcome(
            state="not_read",
            book_a=brief_a, book_b=brief_b,
            note=f"{'、'.join(missing)}还没有通读过——先让我读一遍，我才有章节要点可以对比。",
        )

    pair_key = pair_key_of(book_a.id, book_b.id)
    if not force:
        existing = _cached(session, user_id=user_id, pair_key=pair_key)
        if existing is not None and existing.rows:
            return CompareOutcome(
                state="cached",
                book_a=brief_a, book_b=brief_b,
                overview=existing.overview,
                rows=list(existing.rows or []),
                aligned=list(existing.aligned or []),
                unmatched_a=list(existing.unmatched_a or []),
                unmatched_b=list(existing.unmatched_b or []),
                note="（直接给你上次的对比结果）",
            )

    chapters_a = _normalize_chapters(reading_a.chapters_note)
    chapters_b = _normalize_chapters(reading_b.chapters_note)
    if not chapters_a or not chapters_b:
        return CompareOutcome(
            state="not_read", book_a=brief_a, book_b=brief_b,
            note="两本书的分章要点都没读到，重新通读一次再对比。",
        )

    pairs, unmatched_a, unmatched_b = align(chapters_a, chapters_b)
    if not pairs:
        # 没有共同主题：如实说，不许硬凑
        return CompareOutcome(
            state="no_overlap",
            book_a=brief_a, book_b=brief_b,
            unmatched_a=unmatched_a, unmatched_b=unmatched_b,
            note=(
                "这两本书没有可对齐的共同主题，做逐主题对比只会硬凑。"
                "你可以分别问它们各自讲了什么。"
            ),
        )

    if injected is not None:
        draft = injected
    else:
        gw = gateway or ModelGateway()
        payload = build_payload(
            title_a=title_a, title_b=title_b,
            pairs=pairs, chapters_a=chapters_a, chapters_b=chapters_b,
            unmatched_a=unmatched_a, unmatched_b=unmatched_b,
        )
        try:
            completion = gw.chat(
                task_type="book_compare",
                messages=[
                    {"role": "system", "content": BOOK_COMPARE.text},
                    {"role": "user", "content": payload},
                ],
                user_id=user_id,
                session=session,
                prompt_version=BOOK_COMPARE.version,
                json_model=CompareDraft,
            )
            draft = parse_structured(completion.text, validator=lambda d: CompareDraft(**d))
        except JsonParseError as exc:
            logger.warning("book_compare: 输出无法解析: %s", exc)
            return CompareOutcome(
                state="degraded", book_a=brief_a, book_b=brief_b,
                note="这次没对比出来，重试一下？",
            )
        except Exception as exc:  # noqa: BLE001 - 模型层异常不该炸掉请求
            logger.warning("book_compare: 调用失败: %s", exc)
            return CompareOutcome(
                state="degraded", book_a=brief_a, book_b=brief_b,
                note="这次没对比出来，重试一下？",
            )

    rows = _validate_rows(draft.rows)
    if not rows:
        return CompareOutcome(
            state="degraded", book_a=brief_a, book_b=brief_b,
            note="模型给的结果不合格（缺关系判断或把两边说法写空了），没有保存。",
        )
    rows = _attach_chars(rows, book_a, book_b)

    _save(
        session, user_id=user_id, pair_key=pair_key,
        book_a_id=book_a.id, book_b_id=book_b.id,
        overview=draft.overview.strip()[:600], rows=rows,
        aligned=pairs, unmatched_a=unmatched_a, unmatched_b=unmatched_b,
    )
    return CompareOutcome(
        state="ok",
        book_a=brief_a, book_b=brief_b,
        overview=draft.overview.strip()[:600],
        rows=rows, aligned=pairs,
        unmatched_a=unmatched_a, unmatched_b=unmatched_b,
    )


def _char_of(book: Book, chapter_index: int) -> int:
    """章节序号 → 全文偏移，用于跳回内置阅读器。

    只有内置书才有 `chapters`（带 char_start）。跳不过去就返回 -1，
    前端据此不渲染链接——给一个点了没反应的链接比不给更糟。
    """
    for chapter in book.chapters or []:
        try:
            if int(chapter.get("index", -1)) == int(chapter_index):
                return int(chapter.get("char_start") or 0)
        except (TypeError, ValueError):
            continue
    return -1


def _attach_chars(rows: list[dict], book_a: Book, book_b: Book) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        item["a_char"] = _char_of(book_a, row.get("a_index", -1))
        item["b_char"] = _char_of(book_b, row.get("b_index", -1))
        out.append(item)
    return out


def _validate_rows(rows: list[CompareRow]) -> list[dict]:
    """只保留结构完整的行。

    `difference` 是这一行的意义所在——没有差异说明的"对比"，只是把两段话并排放着。
    """
    kept: list[dict] = []
    for row in rows:
        relation = (row.relation or "").strip()
        if relation not in RELATIONS:
            continue
        if not (row.a_says.strip() and row.b_says.strip()):
            continue
        if not row.difference.strip():
            continue
        kept.append({
            "topic": row.topic.strip()[:120],
            "relation": relation,
            "a_says": row.a_says.strip()[:400],
            "b_says": row.b_says.strip()[:400],
            "difference": row.difference.strip()[:400],
            "a_index": int(row.a_index),
            "b_index": int(row.b_index),
        })
    return kept[:MAX_PAIRS]


def _save(
    session: Session, *, user_id: str, pair_key: str,
    book_a_id: str, book_b_id: str, overview: str, rows: list,
    aligned: list, unmatched_a: list, unmatched_b: list,
) -> None:
    existing = _cached(session, user_id=user_id, pair_key=pair_key)
    if existing is None:
        existing = BookComparison(
            user_id=user_id, book_a_id=book_a_id, book_b_id=book_b_id, pair_key=pair_key
        )
        session.add(existing)
    existing.book_a_id = book_a_id
    existing.book_b_id = book_b_id
    existing.overview = overview
    existing.rows = rows
    existing.aligned = aligned
    existing.unmatched_a = unmatched_a
    existing.unmatched_b = unmatched_b
    session.commit()


def pick_default_pair(session: Session, *, user_id: str) -> tuple[str, str]:
    """最近通读过的两本书（用户没说书名时的兜底）。

    只挑**真的有分章要点**的：通读失败的书拿去做对比，只会得到一句"没有要点"。
    """
    rows = session.scalars(
        select(Book)
        .where(Book.user_id == user_id, Book.is_deleted.is_(False))
        .order_by(Book.updated_at.desc())
        .limit(12)
    ).all()
    readings = BookAgentReadingRepository(session, user_id=user_id)
    picked: list[str] = []
    for book in rows:
        reading = readings.get_by_book(book.id)
        if reading is not None and reading.chapters_note:
            picked.append(book.id)
        if len(picked) >= 2:
            break
    return (picked[0], picked[1]) if len(picked) == 2 else ("", "")


def reply_text(outcome: CompareOutcome) -> str:
    if outcome.state == "not_read":
        return outcome.note
    if outcome.state in ("degraded", "no_overlap", "not_found"):
        return outcome.note
    if outcome.state == "cached":
        return f"《{outcome.book_a.get('title')}》和《{outcome.book_b.get('title')}》的对比（上次的结果）"
    title_a = outcome.book_a.get("title", "")
    title_b = outcome.book_b.get("title", "")
    counts: dict[str, int] = {}
    for row in outcome.rows:
        counts[row["relation"]] = counts.get(row["relation"], 0) + 1
    parts = [f"{k} {v} 处" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
    return (
        f"《{title_a}》和《{title_b}》对上 {len(outcome.rows)} 个主题："
        + "、".join(parts)
    )
