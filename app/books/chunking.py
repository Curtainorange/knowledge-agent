"""书籍原文分块与检索 —— 让「就书讨论」能查到原句，而不是只有通读摘要。

## 解决什么

通读把全书压成几千字笔记，之后讨论只喂这份笔记：用户问「第 3 章那个实验的数据是多少」，
模型手里没有那句话，只能顺着要点编。分块把原文切成 ~1200 字的片段，讨论前先检索，
把最相关的几段连同章节、页码一起送进去。

## 为什么块落库而不每次现切

块边界一旦随切块规则变化而漂移，引用就无法复现：今天说「第 12 段」，改个阈值就变成
「第 11 段」。落库保证同一本书的块编号稳定，也便于以后接向量检索。

## 检索用 BM25 而不是向量

复用 `app/retrieval/bm25.py`（混合检索的 lexical 通道），**零额外依赖、零 API 成本**，
一本书 100~150 块在内存里建索引只要几十毫秒。向量化留到二期——先验证「能查到原句」
这件事本身有没有用，再决定要不要花那份复杂度。
"""
from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.models.book_chunk import BookChunk
from app.retrieval.bm25 import BM25Index

logger = logging.getLogger(__name__)

# 一块约一屏半（中文）。太小则上下文碎片化，太大则检索精度与成本双输。
CHUNK_CHARS = 1200
# 尾块短于这个比例就并进前一块：孤零零的两百字既检索不准，也白占一条记录
CHUNK_MIN_TAIL_RATIO = 0.35
# 切点回退窗口：允许在目标位置之前 25% 的范围内找段落 / 句末边界
CHUNK_CUT_WINDOW_RATIO = 0.25
# 送回模型的片段数
SEARCH_TOP_K = 3
# 相对阈值：只保留分数不低于最高分这个比例的块。
# 用相对而不是绝对，是因为 BM25 的分数尺度取决于书的词表大小，跨书不可比。
SEARCH_RELATIVE_FLOOR = 0.25

_CUT_SEPARATORS = ("\n\n", "。", "！", "？", "\n")


def _cut_spans(text: str, start: int, end: int, size: int) -> list[tuple[int, int]]:
    """把 [start, end) 切成若干块，切点尽量落在段落 / 句末，不硬切在句子中间。"""
    spans: list[tuple[int, int]] = []
    pos = start
    window_size = max(1, int(size * CHUNK_CUT_WINDOW_RATIO))
    while pos < end:
        stop = min(pos + size, end)
        if stop < end:
            floor = max(pos + 1, stop - window_size)
            window = text[floor:stop]
            # 取最靠右的分隔符：越靠后越接近目标长度，块大小更均匀
            best = -1
            for separator in _CUT_SEPARATORS:
                index = window.rfind(separator)
                if index > best:
                    best = index
            if best >= 0:
                stop = floor + best + 1
        if stop <= pos:  # 防御：分隔符紧贴起点时不能原地打转
            stop = min(pos + size, end)
        spans.append((pos, stop))
        pos = stop

    # 尾块太短就并进前一块
    if len(spans) >= 2:
        tail_start, tail_end = spans[-1]
        if (tail_end - tail_start) < size * CHUNK_MIN_TAIL_RATIO:
            spans.pop()
            spans[-1] = (spans[-1][0], tail_end)
    return spans


def split_book(book: Book, *, size: int = CHUNK_CHARS) -> list[dict]:
    """把一本书切成块 → [{seq, chapter_index, chapter_title, char_start, char_end, text}]。

    只在章节内部切：跨章的块会让引用坐标（第几章）失去意义。
    章节本身小于一块时直接整章成块。
    """
    full_text = book.full_text or ""
    if not full_text.strip():
        return []

    chapters = sorted(
        (
            c for c in (book.chapters or [])
            if int(c.get("char_end") or 0) > int(c.get("char_start") or 0)
        ),
        key=lambda c: int(c.get("index") or 0),
    )
    if not chapters:
        chapters = [{"index": 1, "title": "", "char_start": 0, "char_end": len(full_text)}]

    chunks: list[dict] = []
    for chapter in chapters:
        start = max(0, int(chapter.get("char_start") or 0))
        end = min(len(full_text), int(chapter.get("char_end") or 0))
        if end <= start:
            continue
        title = str(chapter.get("title") or "")
        index = int(chapter.get("index") or 0)
        for span_start, span_end in _cut_spans(full_text, start, end, size):
            body = full_text[span_start:span_end]
            if not body.strip():
                continue  # 纯空白块（目录页 / 分页符残留）没有检索价值
            chunks.append({
                "seq": len(chunks),
                "chapter_index": index,
                "chapter_title": title,
                "char_start": span_start,
                "char_end": span_end,
                "text": body,
            })
    return chunks


def rebuild(session: Session, book: Book) -> int:
    """重建一本书的全部块（先删后建），返回块数。"""
    session.execute(delete(BookChunk).where(BookChunk.book_id == book.id))
    chunks = split_book(book)
    for chunk in chunks:
        session.add(BookChunk(user_id=book.user_id, book_id=book.id, **chunk))
    session.flush()
    return len(chunks)


def ensure_chunks(session: Session, book: Book) -> int:
    """确保这本书有块索引；没有就建一次（老书首次讨论时懒回填）。"""
    existing = session.scalar(
        select(BookChunk.id).where(BookChunk.book_id == book.id).limit(1)
    )
    if existing is not None:
        return 0
    return rebuild(session, book)


def _page_label(book: Book, start: int, end: int) -> str:
    """块的字符区间 → 页码标注（引用用）。

    一个字块通常跨 2~3 页，所以标的是**区间**而不是起点页：
    只标起点会把「第 86 页」贴在一段横跨 86~88 页的原文上，读者按它翻书找不到。
    两端都有印刷页码时用 `P.86–88`；没有（扫描件 / 页码没识别出来）退回物理页。
    """
    marks: list[tuple[int, int, str]] = []
    for mark in book.page_marks or []:
        try:
            at = int(mark.get("char_start") or 0)
        except (TypeError, ValueError):
            continue
        marks.append((at, int(mark.get("page") or 0), str(mark.get("label") or "")))
    if not marks:
        return ""
    marks.sort(key=lambda item: item[0])

    def page_at(position: int) -> tuple[int, int, str]:
        picked = marks[0]
        for mark in marks:
            if mark[0] <= position:
                picked = mark
            else:
                break
        return picked

    head = page_at(start)
    tail = page_at(max(start, end - 1))
    if head[2] and tail[2]:
        return f"P.{head[2]}" if head[2] == tail[2] else f"P.{head[2]}–{tail[2]}"
    if head[1] == tail[1]:
        return f"PDF 第 {head[1]} 页"
    return f"PDF 第 {head[1]}–{tail[1]} 页"


def search(
    session: Session, *, book: Book, query: str, top_k: int = SEARCH_TOP_K
) -> list[dict]:
    """在一本书里检索相关片段。

    返回 [{chapter_title, page, char_start, char_end, text, score}]，按分数降序。
    查不到（或查询太泛）时返回空表——调用方据此只用通读笔记，绝不塞无关段落。
    """
    rows = list(
        session.scalars(
            select(BookChunk)
            .where(BookChunk.book_id == book.id, BookChunk.user_id == book.user_id)
            .order_by(BookChunk.seq)
        )
    )
    if not rows or not (query or "").strip():
        return []

    index = BM25Index()
    for row in rows:
        index.add(row.id, title=row.chapter_title or "", content=row.text or "")
    scores = index.score(query)
    if not scores:
        return []

    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[: top_k * 2]
    best = ranked[0][1]
    floor = best * SEARCH_RELATIVE_FLOOR
    by_id = {row.id: row for row in rows}

    hits: list[dict] = []
    for chunk_id, score in ranked:
        if score < floor:
            continue
        row = by_id.get(chunk_id)
        if row is None:
            continue
        hits.append({
            "chapter_title": row.chapter_title or "",
            "chapter_index": row.chapter_index,
            "page": _page_label(book, row.char_start, row.char_end),
            "char_start": row.char_start,
            "char_end": row.char_end,
            "text": row.text,
            "score": round(score, 4),
        })
        if len(hits) >= top_k:
            break
    return hits
