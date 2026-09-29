"""书籍三项编排：通读全书（book_digest）、就书讨论（book_discuss）、推荐书籍（book_recommend）。

**与用户阅读的隔离是这条线的第一设计约束**：通读产出只写 `book_agent_readings`，
不碰 `books.read_progress / current_char`，也不往 `knowledge_items` 里塞任何东西——
「智能体读过的书」和「你读过的书」在数据层就是两本账。

**通读的做法（刻意只用逐块 + 一次汇总，不引入长上下文/向量检索）：**

1. 全文按章节（无章节按定长）切成 ~1.2 万字的块，逐块让模型产出
   「概括 + 要点」（结构化 JSON，reasoning=off）；
2. 各块要点再汇总成一份全书总评；
3. 结果覆盖式落库：每个 (user, book) 只保留最新一份，且记录当时的 `total_chars`——
   书没变（没有重新上传）就直接复用，不重复花钱重读。

成本上限是硬的：超过 `MAX_CHUNKS` 块的书截断（卡上注明），单块要点提取失败跳过
（汇总照做，卡上注明有几块没读成）——通读是「尽力而为的上下文建设」，不是必须满分的任务。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.book_agent_reading_repository import BookAgentReadingRepository
from app.domain.repositories.book_repository import BookRepository
from app.feedback import events
from app.llm.completion import Completion
from app.llm.exceptions import LLMError
from app.llm.gateway import ModelGateway
from app.llm.prompts import BOOK_CHAT, BOOK_DIGEST_CHUNK, BOOK_DIGEST_SUMMARY, BOOK_RECOMMEND

logger = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 12_000   # 单块最大字符数（送模型的原文长度）
MAX_CHUNKS = 24            # 块数上限：28.8 万字封顶，超出截断并在卡片注明
                           # （19.9 万字的《刻意练习》= 17 块，16 挡不住真实书单）


# ---- 模型输出契约（结构化 JSON）---------------------------------------------


class ChunkDigest(BaseModel):
    """一块原文的阅读笔记。topic 是领域标签（2-6 字），供 L2 影子主张走主题通道。"""

    topic: str = ""
    gist: str = ""
    points: list[str] = Field(default_factory=list)


class SummaryDigest(BaseModel):
    """全书总评。"""

    summary: str = ""


class BookRecommendation(BaseModel):
    title: str
    author: str = ""
    fit: str = ""
    reason: str = ""


class BookRecommendList(BaseModel):
    overview: str = ""
    items: list[BookRecommendation] = Field(default_factory=list)


@dataclass
class DigestOutcome:
    """通读结果：turns / cards 层只消费这个形状，不感知块切分细节。"""

    state: str                       # ok | reused | degraded
    book_id: str
    title: str
    author: str
    summary: str
    chapters: list[dict] = field(default_factory=list)
    chunk_count: int = 0
    failed_chunks: int = 0
    truncated: bool = False
    note: str = ""


@dataclass
class RecommendOutcome:
    state: str                       # ok | empty | degraded
    overview: str = ""
    items: list[BookRecommendation] = field(default_factory=list)
    note: str = ""


# ---- 选书 ------------------------------------------------------------------


def resolve_book(session: Session, *, user_id: str, title: str) -> Book | None:
    """按书名在书架里找书：精确命中 → 唯一包含命中；找不到或有歧义返回 None。

    「唯一包含」是双向的：用户说《三体》能命中《三体（全三册）》，
    说全名也能命中简名。有歧义时不猜——返回 None 让上层出选择卡。
    """
    wanted = (title or "").strip()
    if not wanted:
        return None
    books = BookRepository(session, user_id=user_id).list_active(user_id)
    exact = [b for b in books if b.title.strip() == wanted]
    if len(exact) == 1:
        return exact[0]
    contains = [b for b in books if wanted in b.title or b.title in wanted]
    return contains[0] if len(contains) == 1 else None


# ---- 通读 ------------------------------------------------------------------


def _build_chunks(book: Book) -> tuple[list[dict], bool]:
    """把全文切成 (块列表, 是否截断)。块：{index, title, text}。

    切块的规模上限是**字数**而不是章节数——epub 章节碎片很多（目录、版权页都是一章），
    若按章切，块上限会被碎片顶满，正文反而被截断。做法：章节先展开成片段，
    再把相邻小片段贪心合并到接近 `MAX_CHUNK_CHARS`。
    """
    full_text = book.full_text or ""
    pieces: list[tuple[str, str]] = []

    chapters = [c for c in (book.chapters or []) if (c.get("char_end", 0) - c.get("char_start", 0)) > 0]
    if len(chapters) >= 2:
        for chapter in sorted(chapters, key=lambda c: c.get("index", 0)):
            text = full_text[chapter["char_start"]:chapter["char_end"]]
            title = chapter.get("title") or f"第 {chapter.get('index', '')} 章"
            spans = list(range(0, len(text), MAX_CHUNK_CHARS))
            for offset in spans:
                label = title if len(spans) == 1 else f"{title}（{offset // MAX_CHUNK_CHARS + 1}）"
                pieces.append((label, text[offset:offset + MAX_CHUNK_CHARS]))
    else:
        for offset in range(0, len(full_text), MAX_CHUNK_CHARS):
            pieces.append((f"第 {offset // MAX_CHUNK_CHARS + 1} 段", full_text[offset:offset + MAX_CHUNK_CHARS]))

    # 贪心合并相邻片段：单块不超过 MAX_CHUNK_CHARS
    chunks: list[dict] = []
    for label, text in pieces:
        if chunks and len(chunks[-1]["text"]) + len(text) <= MAX_CHUNK_CHARS:
            last = chunks[-1]
            last["text"] += text
            last["titles"].append(label)
        else:
            chunks.append({"index": len(chunks), "title": label, "text": text, "titles": [label]})

    truncated = len(chunks) > MAX_CHUNKS
    chunks = chunks[:MAX_CHUNKS]
    for chunk in chunks:
        if len(chunk["titles"]) > 1:
            chunk["title"] = f"{chunk['titles'][0]} 等 {len(chunk['titles'])} 节"
        del chunk["titles"]
    return chunks, truncated


def _digest_chunk(gateway: ModelGateway, *, user_id: str, chunk: dict) -> ChunkDigest:
    from app.llm.structure import parse_structured

    completion: Completion = gateway.chat(
        task_type="book_digest",
        messages=[
            {"role": "system", "content": BOOK_DIGEST_CHUNK.text},
            {"role": "user", "content": f"【{chunk['title']}】\n\n{chunk['text']}"},
        ],
        user_id=user_id,
        prompt_version=BOOK_DIGEST_CHUNK.version,
        json_model=ChunkDigest,
    )
    return parse_structured(completion.text, validator=lambda d: ChunkDigest(**d))


def _summarize(gateway: ModelGateway, *, user_id: str, notes: list[dict]) -> SummaryDigest:
    digest_text = "\n\n".join(
        f"【{note['title']}】{note['gist']}\n" + "\n".join(f"- {p}" for p in note["points"])
        for note in notes
    )
    completion: Completion = gateway.chat(
        task_type="book_digest",
        messages=[
            {"role": "system", "content": BOOK_DIGEST_SUMMARY.text},
            {"role": "user", "content": f"书名：《{notes[0]['book_title']}》\n\n{digest_text}"},
        ],
        user_id=user_id,
        prompt_version=BOOK_DIGEST_SUMMARY.version,
        json_model=SummaryDigest,
    )
    from app.llm.structure import parse_structured

    return parse_structured(completion.text, validator=lambda d: SummaryDigest(**d))


def read_whole_book(
    session: Session, gateway: ModelGateway, *, user_id: str, book: Book, force: bool = False
) -> DigestOutcome:
    """通读一本书（或复用未过期的既有笔记），结果覆盖式落 `book_agent_readings`。

    不碰用户的阅读进度与知识库——隔离约束见模块 docstring。
    """
    reading_repo = BookAgentReadingRepository(session, user_id=user_id)

    if not force:
        existing = reading_repo.get_by_book(book.id)
        if (
            existing is not None
            and existing.status == "done"
            and existing.total_chars == book.total_chars
            and (existing.summary or existing.chapters_note)
        ):
            return _outcome_from_reading(book, existing, state="reused")

    chunks, truncated = _build_chunks(book)
    if not chunks:
        return DigestOutcome(
            state="degraded", book_id=book.id, title=book.title, author=book.author or "",
            summary="", note="这本书没有可读的文本内容（可能是空文件或纯图片 PDF）。",
        )

    notes: list[dict] = []
    failed = 0
    for chunk in chunks:
        try:
            digest = _digest_chunk(gateway, user_id=user_id, chunk=chunk)
            notes.append({
                "index": chunk["index"], "title": chunk["title"],
                "book_title": book.title,
                "topic": digest.topic.strip()[:64],
                "gist": digest.gist.strip(),
                "points": [p.strip() for p in digest.points if p and p.strip()][:5],
            })
        except LLMError as exc:
            failed += 1
            logger.warning("book digest chunk failed book=%s chunk=%s err=%s", book.id, chunk["title"], exc)
            notes.append({
                "index": chunk["index"], "title": chunk["title"],
                "book_title": book.title, "topic": "", "gist": "", "points": [],
            })

    usable = [note for note in notes if note["gist"]]
    summary = ""
    if usable:
        try:
            summary = _summarize(gateway, user_id=user_id, notes=usable).summary.strip()
        except LLMError as exc:
            logger.warning("book digest summary failed book=%s err=%s", book.id, exc)

    reading = reading_repo.upsert(
        book_id=book.id,
        status="done" if usable else "failed",
        total_chars=book.total_chars,
        summary=summary,
        chapters_note=[
            {"index": n["index"], "title": n["title"], "topic": n["topic"],
             "gist": n["gist"], "points": n["points"]}
            for n in notes
        ],
        chunk_count=len(chunks),
        failed_chunks=failed,
        truncated=truncated,
    )

    events.record(session, user_id=user_id, event_type=events.BOOK_DIGEST_DONE, payload={
        "book_id": book.id, "chunks": len(chunks), "failed_chunks": failed,
        "truncated": truncated, "state": reading.status,
    })

    state = "ok" if usable else "degraded"
    note = ""
    if failed:
        note = f"有 {failed} 段没能生成要点，其余部分不受影响。"
    if truncated:
        note = (note + " " if note else "") + (
            f"全书超出单次通读上限，本次精读了前 {MAX_CHUNKS} 块（约 "
            f"{sum(len(c['text']) for c in chunks)} 字）。"
        )
    return DigestOutcome(
        state=state, book_id=book.id, title=book.title, author=book.author or "",
        summary=summary, chapters=reading.chapters_note or [],
        chunk_count=len(chunks), failed_chunks=failed, truncated=truncated, note=note,
    )


def _outcome_from_reading(book: Book, reading: BookAgentReading, *, state: str) -> DigestOutcome:
    note = "这本书已经有通读笔记，直接沿用。想重新读一遍就说「重新通读」。" if state == "reused" else ""
    return DigestOutcome(
        state=state, book_id=book.id, title=book.title, author=book.author or "",
        summary=reading.summary or "", chapters=reading.chapters_note or [],
        chunk_count=reading.chunk_count, failed_chunks=reading.failed_chunks,
        truncated=bool(reading.truncated), note=note,
    )


# ---- 讨论 ------------------------------------------------------------------


def discuss(
    session: Session, gateway: ModelGateway, *, user_id: str, book: Book, question: str
) -> str | None:
    """就通读过的书回答一个问题。没有通读笔记返回 None（调用方提示先通读）。

    讨论是自然语言回复（不产 JSON），复用 multi_turn_dialogue 的策略，
    只把系统提示词换成 BOOK_CHAT 并显式传版本。
    """
    reading = BookAgentReadingRepository(session, user_id=user_id).get_by_book(book.id)
    if reading is None or (not reading.summary and not reading.chapters_note):
        return None

    chapters = [
        f"【{c.get('title', '')}】{c.get('gist', '')}"
        + ("".join(f"\n- {p}" for p in (c.get("points") or [])))
        for c in (reading.chapters_note or [])
        if c.get("gist")
    ]
    notes_text = (f"总评：{reading.summary}\n\n" if reading.summary else "") + "\n\n".join(chapters)
    notes_text = notes_text[:16_000]  # 笔记超长时截断：讨论够用，且不影响落库的完整版

    completion: Completion = gateway.chat(
        task_type="multi_turn_dialogue",
        messages=[
            {"role": "system", "content": BOOK_CHAT.text},
            {
                "role": "user",
                "content": f"书名：《{book.title}》\n\n【通读笔记】\n{notes_text}\n\n"
                           f"【用户的问题】\n{question}",
            },
        ],
        user_id=user_id,
        prompt_version=BOOK_CHAT.version,
    )
    return completion.text.strip()


# ---- 推荐 ------------------------------------------------------------------


def _profile(session: Session, *, user_id: str) -> dict:
    """学习画像（纯本地聚合）：主题标签、最近条目、书架、目标、已通读的书。"""
    from collections import Counter

    tag_counter: Counter = Counter()
    recent_titles: list[str] = []
    rows = session.execute(
        select(KnowledgeItem.title, KnowledgeItem.tags).where(
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.is_deleted.is_(False),
        )
    ).all()
    for title, tags in rows:
        tag_counter.update(t.strip() for t in (tags or []) if t and t.strip())
        if title:
            recent_titles.append(title)

    books = [
        {"title": b.title, "author": b.author or "", "progress": round(float(b.read_progress or 0.0), 2)}
        for b in BookRepository(session, user_id=user_id).list_active(user_id)
    ]

    from app.domain.repositories.learning_plan_repository import LearningGoalRepository

    goals = [g.description for g in LearningGoalRepository(session, user_id=user_id).list_active(user_id)]

    from app.domain.models.book_agent_reading import BookAgentReading as _R

    digested = [
        row[0]
        for row in session.execute(
            select(Book.title).join(_R, _R.book_id == Book.id).where(
                _R.user_id == user_id, _R.status == "done", _R.is_deleted.is_(False)
            )
        ).all()
    ]

    return {
        "top_tags": [tag for tag, _count in tag_counter.most_common(12)],
        "recent_items": recent_titles[-10:],
        "books": books,
        "goals": [g for g in goals if g],
        "agent_read_books": digested,
    }


def recommend(session: Session, gateway: ModelGateway, *, user_id: str) -> RecommendOutcome:
    """根据学习画像推荐书单（不联网，靠模型自己的书目知识）。"""
    profile = _profile(session, user_id=user_id)
    has_anything = (
        profile["top_tags"] or profile["recent_items"] or profile["books"] or profile["goals"]
    )
    if not has_anything:
        return RecommendOutcome(
            state="empty",
            note="知识库和书架都还空着——先记几条知识或传一本书，推荐才有依据。",
        )

    import json as _json

    try:
        completion: Completion = gateway.chat(
            task_type="book_recommend",
            messages=[
                {"role": "system", "content": BOOK_RECOMMEND.text},
                {
                    "role": "user",
                    "content": "学习画像 JSON：\n" + _json.dumps(profile, ensure_ascii=False),
                },
            ],
            user_id=user_id,
            prompt_version=BOOK_RECOMMEND.version,
            json_model=BookRecommendList,
        )
        from app.llm.structure import parse_structured

        result = parse_structured(completion.text, validator=lambda d: BookRecommendList(**d))
    except LLMError as exc:
        logger.warning("book recommend failed user=%s err=%s", user_id, exc)
        raise

    events.record(session, user_id=user_id, event_type=events.BOOK_RECOMMEND_DONE, payload={
        "count": len(result.items),
    })
    return RecommendOutcome(state="ok", overview=result.overview, items=result.items[:5])
