"""书籍原文分块与检索（A1）。

这条能力的价值全在「引用能不能对上原文」上，所以测试重点不是「跑了不报错」，
而是三件硬约束：

1. 块**不跨章**——跨章的块会让「第几章」这个引用坐标失去意义；
2. 块的 `char_start/char_end` **必须能在 full_text 上原样切回自己**——
   引用、跳回原文、页码换算全靠这个偏移，错一位就全歪；
3. 检索命中的必须是**真的相关**的块，查不到就老老实实返回空
   （宁可只用摘要，也不塞无关段落污染回答）。
"""
from __future__ import annotations

import pytest

from app.books import chunking
from app.domain.models.book import Book


def _book(text: str, *, chapters=None, page_marks=None) -> Book:
    return Book(
        id="bk1",
        user_id="u1",
        title="测试书",
        author="",
        format="txt",
        file_path="",
        chapters=chapters or [],
        full_text=text,
        page_marks=page_marks or [],
        total_chars=len(text),
    )


def test_split_does_not_cross_chapters():
    """块只在章内切：跨章的块没法说清「这是第几章」。"""
    first, second = "甲" * 10, "乙" * 10
    text = f"{first}\n\n{second}"
    chapters = [
        {"index": 1, "title": "一", "char_start": 0, "char_end": 10},
        {"index": 2, "title": "二", "char_start": 12, "char_end": 22},
    ]

    chunks = chunking.split_book(_book(text, chapters=chapters), size=100)

    assert [c["chapter_index"] for c in chunks] == [1, 2]
    assert chunks[0]["text"] == first
    assert chunks[1]["text"] == second


def test_chunk_offsets_cut_back_from_full_text():
    """偏移必须能原样切回块内容——这是引用与页码换算的地基。"""
    body = "".join(f"第{i}段的内容写得比较长一些，用来凑够切块长度。" for i in range(60))
    book = _book(body)

    chunks = chunking.split_book(book, size=200)

    assert len(chunks) > 1
    for chunk in chunks:
        assert book.full_text[chunk["char_start"]:chunk["char_end"]] == chunk["text"]
    # 连续覆盖，不重不漏
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(body)
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev["char_end"] == nxt["char_start"]


def test_cut_prefers_sentence_end():
    """切点落在句末（。！？换行），不硬断在句子中间。"""
    sentence = "这是一句话，长度刚好够用来测试切点位置。"
    text = sentence * 40

    for chunk in chunking.split_book(_book(text), size=200):
        if chunk["char_end"] < len(text):
            assert chunk["text"].rstrip()[-1] in "。！？\n", chunk["text"][-20:]


def test_short_tail_is_merged_into_previous_chunk():
    """尾块太短就并进前一块：孤零零两百字既检索不准也白占一条记录。"""
    text = "甲" * 1300

    chunks = chunking.split_book(_book(text), size=1200)

    assert len(chunks) == 1
    assert len(chunks[0]["text"]) == 1300


def test_empty_book_produces_no_chunks():
    assert chunking.split_book(_book("   ")) == []


def test_book_without_chapters_is_chunked_whole():
    """没有章节信息的书（txt 单章）也要能切块，不能直接放弃。"""
    text = "内容。" * 800
    chunks = chunking.split_book(_book(text), size=300)

    assert chunks and all(c["chapter_index"] == 1 for c in chunks)


# ---------- 检索 ----------

def test_search_hits_matching_chunk(session):
    book = _book(
        "第一章 起点\n刻意练习的核心是有目标的练习。\n\n"
        "第二章 心理表征\n心理表征是大脑对事物的内部编码方式，高手靠它预判局面。",
        chapters=[
            {"index": 1, "title": "起点", "char_start": 0, "char_end": 22},
            {"index": 2, "title": "心理表征", "char_start": 24, "char_end": 62},
        ],
    )
    session.add(book)
    session.flush()
    chunking.rebuild(session, book)

    hits = chunking.search(session, book=book, query="心理表征是什么意思")

    assert hits, "应命中「心理表征」那一章"
    assert "心理表征" in hits[0]["text"]
    assert hits[0]["chapter_title"] == "心理表征"
    assert hits[0]["char_start"] >= 24


def test_search_returns_empty_when_nothing_matches(session):
    """查不到就返回空：调用方据此只用摘要，不塞无关段落。"""
    book = _book("第一章\n这是关于数据库索引与查询优化的内容。", chapters=[
        {"index": 1, "title": "数据库", "char_start": 0, "char_end": 23},
    ])
    session.add(book)
    session.flush()
    chunking.rebuild(session, book)

    assert chunking.search(session, book=book, query="红楼梦大观园诗词") == []


def test_search_reports_page_range_for_pdf(session):
    """PDF 书的命中片段要标出原书页码区间：块跨页时只标起点会指错地方。"""
    text = "开头一句话。这一页讲的是刻意练习的反馈机制，还有后续的展开说明。"
    book = _book(
        text,
        chapters=[{"index": 1, "title": "开头", "char_start": 0, "char_end": len(text)}],
        page_marks=[
            {"page": 1, "char_start": 0, "label": "86"},
            {"page": 2, "char_start": 7, "label": "87"},
        ],
    )
    session.add(book)
    session.flush()
    chunking.rebuild(session, book)

    hits = chunking.search(session, book=book, query="刻意练习的反馈机制")

    assert hits
    # 整块横跨第 1、2 页（原书 P.86、P.87）→ 必须标成区间
    assert hits[0]["page"] == "P.86–87"


def test_search_page_label_single_page(session):
    """块只在一页内时标单页页码，不画蛇添足标区间。"""
    text = "这一页完整讲完了反馈机制这件事。"
    book = _book(
        text,
        chapters=[{"index": 1, "title": "开头", "char_start": 0, "char_end": len(text)}],
        page_marks=[{"page": 1, "char_start": 0, "label": "12"}],
    )
    session.add(book)
    session.flush()
    chunking.rebuild(session, book)

    hits = chunking.search(session, book=book, query="反馈机制")

    assert hits and hits[0]["page"] == "P.12"


def test_ensure_chunks_is_idempotent(session):
    """已有块的书不再重建：块编号稳定，引用才可复现。"""
    book = _book("内容。" * 300, chapters=[
        {"index": 1, "title": "一", "char_start": 0, "char_end": 900},
    ])
    session.add(book)
    session.flush()

    first = chunking.ensure_chunks(session, book)
    assert first > 0
    assert chunking.ensure_chunks(session, book) == 0
