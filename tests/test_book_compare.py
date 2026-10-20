"""C 模块测试：两本书对比（对齐 / 校验 / 缓存 / 路由）。

对齐是纯函数，重点钉住「不许硬凑」：两本书没有共同主题时必须如实说没有，
而不是让模型编几组对比出来。
"""
from __future__ import annotations

import pytest

from app.agent import book_compare
from app.agent.book_compare import CompareDraft, CompareRow
from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading


def _book(session, *, title: str, user_id: str = "u1", chapters: list | None = None) -> Book:
    book = Book(
        user_id=user_id, title=title, author="", format="txt", file_path=f"{title}.txt",
        full_text="正文" * 500, total_chars=1000,
        chapters=chapters if chapters is not None else [
            {"index": 1, "title": "索引与查询优化", "char_start": 0, "char_end": 300},
            {"index": 2, "title": "事务与隔离级别", "char_start": 300, "char_end": 600},
        ],
    )
    session.add(book)
    session.commit()
    return book


def _reading(session, book: Book, chapters_note: list, *, user_id: str = "u1") -> BookAgentReading:
    row = BookAgentReading(
        user_id=user_id, book_id=book.id, status="done",
        total_chars=book.total_chars, summary="总评",
        chapters_note=chapters_note, chunk_count=3, failed_chunks=0, truncated=False,
    )
    session.add(row)
    session.commit()
    return row


def _chapters(*titles: str) -> list[dict]:
    return [
        {"index": i, "title": t, "gist": f"{t}的要点", "points": [f"{t}细节"]}
        for i, t in enumerate(titles, 1)
    ]


# ---- 相似度与对齐 -----------------------------------------------------------


def test_similarity_identical_is_one():
    assert book_compare.topic_similarity("索引与查询优化", "索引与查询优化") == 1.0


def test_similarity_unrelated_is_zero():
    assert book_compare.topic_similarity("索引优化", "番茄炒蛋") == 0.0


def test_similarity_partial_between():
    score = book_compare.topic_similarity("索引与查询优化", "查询优化实践")
    assert 0 < score < 1


def test_align_pairs_matching_topics():
    a = [{"index": 1, "topic": "索引与查询优化", "title": "索引与查询优化", "gist": "", "pos": 0}]
    b = [{"index": 1, "topic": "索引与查询优化", "title": "索引与查询优化", "gist": "", "pos": 0}]
    pairs, only_a, only_b = book_compare.align(a, b)
    assert len(pairs) == 1
    assert not only_a and not only_b


def test_align_does_not_reuse_chapters():
    """一章只能配一次——否则一个总述章节会把对方所有章节都"对上"。"""
    a = [{"index": 1, "topic": "索引优化", "title": "索引优化", "gist": "", "pos": 0}]
    b = [
        {"index": 1, "topic": "索引优化", "title": "索引优化", "gist": "", "pos": 0},
        {"index": 2, "topic": "索引优化实践", "title": "索引优化实践", "gist": "", "pos": 1},
    ]
    pairs, _, only_b = book_compare.align(a, b)
    assert len(pairs) == 1
    assert len(only_b) == 1


def test_align_returns_unmatched_on_both_sides():
    a = [{"index": 1, "topic": "索引优化", "title": "索引优化", "gist": "", "pos": 0},
         {"index": 2, "topic": "番茄炒蛋", "title": "番茄炒蛋", "gist": "", "pos": 1}]
    b = [{"index": 1, "topic": "索引优化", "title": "索引优化", "gist": "", "pos": 0},
         {"index": 2, "topic": "如何养猫", "title": "如何养猫", "gist": "", "pos": 1}]
    pairs, only_a, only_b = book_compare.align(a, b)
    assert len(pairs) == 1
    assert [x["title"] for x in only_a] == ["番茄炒蛋"]
    assert [x["title"] for x in only_b] == ["如何养猫"]


def test_align_no_overlap_when_nothing_similar():
    a = [{"index": 1, "topic": "索引优化", "title": "索引优化", "gist": "", "pos": 0}]
    b = [{"index": 1, "topic": "如何养猫", "title": "如何养猫", "gist": "", "pos": 0}]
    pairs, only_a, only_b = book_compare.align(a, b)
    assert pairs == []
    assert only_a and only_b


# ---- 书名解析 ---------------------------------------------------------------


def test_parse_titles_from_brackets():
    assert book_compare.parse_titles("帮我对比《原则》和《穷查理宝典》") == ("原则", "穷查理宝典")


def test_parse_titles_from_plain_sentence():
    a, b = book_compare.parse_titles("对比一下原则 和 穷查理宝典")
    assert a == "原则" and b == "穷查理宝典"


def test_parse_titles_missing_returns_empty():
    assert book_compare.parse_titles("对比这两本书") == ("", "")


def test_pair_key_is_order_insensitive():
    assert book_compare.pair_key_of("b1", "b2") == book_compare.pair_key_of("b2", "b1")


# ---- 结果校验 ---------------------------------------------------------------


def test_validate_drops_unknown_relation():
    rows = book_compare._validate_rows([
        CompareRow(topic="t", relation="差不多", a_says="x", b_says="y", difference="z")
    ])
    assert rows == []


def test_validate_drops_empty_difference():
    """只把两段话并排放着，不叫对比。"""
    rows = book_compare._validate_rows([
        CompareRow(topic="t", relation="一致", a_says="x", b_says="y", difference="")
    ])
    assert rows == []


def test_validate_drops_missing_side():
    rows = book_compare._validate_rows([
        CompareRow(topic="t", relation="一致", a_says="x", b_says="", difference="z")
    ])
    assert rows == []


def test_validate_keeps_good_row():
    rows = book_compare._validate_rows([
        CompareRow(topic="索引", relation="冲突", a_says="x", b_says="y", difference="前提不同", a_index=1, b_index=2)
    ])
    assert len(rows) == 1 and rows[0]["relation"] == "冲突"


# ---- 主流程 -----------------------------------------------------------------


def _draft() -> CompareDraft:
    return CompareDraft(
        overview="两本在索引上一致，在范式上有冲突",
        rows=[
            CompareRow(topic="索引与查询优化", relation="一致", a_says="按访问模式选结构",
                       b_says="先看慢查询再定索引", difference="结论相同、讲法不同", a_index=1, b_index=1),
        ],
    )


def test_compare_ok_and_persists(session):
    a = _book(session, title="数据库系统概念")
    b = _book(session, title="高性能 MySQL")
    _reading(session, a, _chapters("索引与查询优化", "事务与隔离级别"))
    _reading(session, b, _chapters("索引与查询优化", "反范式的时机"))

    outcome = book_compare.compare(
        session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()}
    )
    assert outcome.state == "ok"
    assert len(outcome.rows) == 1
    assert outcome.aligned


def test_second_call_hits_cache(session):
    a = _book(session, title="A 书")
    b = _book(session, title="B 书")
    _reading(session, a, _chapters("索引与查询优化"))
    _reading(session, b, _chapters("索引与查询优化"))

    book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()})
    again = book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=b.id)
    assert again.state == "cached"
    assert again.rows


def test_force_bypasses_cache(session):
    a = _book(session, title="A 书")
    b = _book(session, title="B 书")
    _reading(session, a, _chapters("索引与查询优化"))
    _reading(session, b, _chapters("索引与查询优化"))
    book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()})

    forced = book_compare.compare(
        session, user_id="u1", book_a_id=a.id, book_b_id=b.id,
        force=True, context={"draft": _draft()},
    )
    assert forced.state == "ok"


def test_not_read_when_missing_reading(session):
    a = _book(session, title="A 书")
    b = _book(session, title="B 书")
    _reading(session, a, _chapters("索引与查询优化"))
    outcome = book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=b.id)
    assert outcome.state == "not_read"
    assert "B 书" in outcome.note


def test_no_overlap_refuses_to_fake_comparison(session):
    """两本书没有共同主题时，必须说没有——不许让模型硬凑几组对比。"""
    a = _book(session, title="数据库系统概念")
    b = _book(session, title="家常菜谱")
    _reading(session, a, _chapters("索引与查询优化"))
    _reading(session, b, _chapters("番茄炒蛋的做法"))

    outcome = book_compare.compare(
        session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()}
    )
    assert outcome.state == "no_overlap"
    assert outcome.rows == []


def test_same_book_rejected(session):
    a = _book(session, title="A 书")
    _reading(session, a, _chapters("索引与查询优化"))
    outcome = book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=a.id)
    assert outcome.state == "not_found"


def test_other_users_book_not_found(session):
    a = _book(session, title="A 书", user_id="u2")
    b = _book(session, title="B 书")
    _reading(session, b, _chapters("索引与查询优化"))
    outcome = book_compare.compare(session, user_id="u1", book_a_id=a.id, book_b_id=b.id)
    assert outcome.state == "not_found"


def test_rows_carry_reader_offsets(session):
    """行里带全文偏移，前端才能跳回内置阅读器。"""
    a = _book(session, title="A 书")
    b = _book(session, title="B 书")
    _reading(session, a, _chapters("索引与查询优化"))
    _reading(session, b, _chapters("索引与查询优化"))
    outcome = book_compare.compare(
        session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()}
    )
    assert outcome.rows[0]["a_char"] >= 0


def test_reply_text_summarises_relations(session):
    a = _book(session, title="A 书")
    b = _book(session, title="B 书")
    _reading(session, a, _chapters("索引与查询优化"))
    _reading(session, b, _chapters("索引与查询优化"))
    outcome = book_compare.compare(
        session, user_id="u1", book_a_id=a.id, book_b_id=b.id, context={"draft": _draft()}
    )
    text = book_compare.reply_text(outcome)
    assert "一致" in text


# ---- 路由 -------------------------------------------------------------------


def test_router_detects_compare_with_titles():
    from app.agent.router import _match_local

    route = _match_local("帮我对比《原则》和《穷查理宝典》")
    assert route is not None
    assert route.capability == "book_compare"
    assert route.args["book_a"] == "原则"
    assert route.args["book_b"] == "穷查理宝典"


def test_router_compare_without_titles():
    from app.agent.router import _match_local

    route = _match_local("对比这两本书")
    assert route is not None
    assert route.capability == "book_compare"
    assert route.args["book_a"] == ""
