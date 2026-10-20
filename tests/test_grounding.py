"""A 模块测试：依据装配与校验（grounding）+ 入库来源白名单 + 内置书去重。

对应 `docs/A模块-知识库同步与基础交互设计-2026-10-20.md` 的 AT-A-01~16。
全程 HashEmbedding + 内存向量库，不触网、不加载大模型。
"""
from __future__ import annotations

import pytest

from app.agent import grounding
from app.books.service import BookService
from app.domain.models.book import Book
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.ingestion.service import KNOWN_SOURCES, IngestionService
from app.retrieval.embedding import HashEmbedding


def _svc(session) -> IngestionService:
    return IngestionService(session, HashEmbedding())


def _book(session, *, user_id="u1", title="原则", text="极度透明意味着组织里没有不能说的话。") -> Book:
    book = Book(
        user_id=user_id,
        title=title,
        author="瑞·达利欧",
        format="txt",
        file_path="x.txt",
        chapters=[{"index": 0, "title": "第一章", "char_start": 0, "char_end": len(text)}],
        full_text=text,
        total_chars=len(text),
    )
    session.add(book)
    session.commit()
    return book


# ---- AT-A-01 / 02：来源白名单 -------------------------------------------------


def test_known_sources_are_exactly_three():
    """来源是枚举，不是自由文本。模型注释里的 notion/obsidian 等是预留、未实现。"""
    assert KNOWN_SOURCES == frozenset({"manual", "book", "weread"})


@pytest.mark.parametrize("source", ["manual", "book", "weread"])
def test_add_knowledge_accepts_whitelisted_sources(session, source):
    item = _svc(session).add_knowledge(user_id="u1", title="标题", content="内容", source=source)
    assert item.source == source


@pytest.mark.parametrize("source", ["notion", "obsidian", "feishu", "bookmark", "agent"])
def test_add_knowledge_rejects_unknown_source(session, source):
    with pytest.raises(ValueError, match="未知知识来源"):
        _svc(session).add_knowledge(user_id="u1", title="标题", content="内容", source=source)
    assert KnowledgeRepository(session, user_id="u1").count_active("u1") == 0


# ---- AT-A-03：内置书去重 ------------------------------------------------------


def test_add_note_dedupes_same_range_and_text(session):
    book = _book(session)
    svc = BookService(session)

    first = svc.add_note(
        user_id="u1", book_id=book.id, text="极度透明意味着组织里没有不能说的话。",
        chapter_index=0, char_start=0, char_end=20,
    )
    assert first is not None and svc.last_note_deduped is False

    second = svc.add_note(
        user_id="u1", book_id=book.id, text="极度透明意味着组织里没有不能说的话。",
        chapter_index=0, char_start=0, char_end=20,
    )
    assert second is not None
    assert second.id == first.id
    assert svc.last_note_deduped is True
    assert KnowledgeRepository(session, user_id="u1").count_active("u1") == 1


def test_add_note_keeps_same_text_at_different_range(session):
    """同一段文字出现在书的两个位置是两处独立摘录，不该合并。"""
    book = _book(session)
    svc = BookService(session)
    svc.add_note(user_id="u1", book_id=book.id, text="极度透明", char_start=0, char_end=4)
    svc.add_note(user_id="u1", book_id=book.id, text="极度透明", char_start=10, char_end=14)
    assert KnowledgeRepository(session, user_id="u1").count_active("u1") == 2


def test_add_note_keeps_different_thought_as_new_item(session):
    """同原文 + 不同想法 = 两条不同条目（后写的想法不能被吃掉）。"""
    book = _book(session)
    svc = BookService(session)
    svc.add_note(user_id="u1", book_id=book.id, text="极度透明", note="想到团队周会")
    svc.add_note(user_id="u1", book_id=book.id, text="极度透明", note="想到一对一沟通")
    assert KnowledgeRepository(session, user_id="u1").count_active("u1") == 2


# ---- AT-A-08~11：片段抽取与溯源 ----------------------------------------------


def test_extract_quote_is_substring_of_content():
    content = "第一句话说别的。第二句讲查询优化与 B+ 树的关系。第三句也无关。"
    quote = grounding.extract_quote(content, "查询优化")
    assert quote in content
    assert "B+ 树" in quote


def test_extract_quote_prefers_matching_sentence():
    content = "开场白。数据库索引的核心是减少磁盘寻道。收尾。"
    assert "磁盘寻道" in grounding.extract_quote(content, "磁盘寻道")


def test_extract_quote_empty_content():
    assert grounding.extract_quote("", "任意") == ""


def test_extract_quote_respects_max_len():
    content = "很长的内容" * 100
    quote = grounding.extract_quote(content, "", max_len=40)
    assert len(quote) <= 40
    assert quote in content


def test_verify_quote_detects_fabrication():
    assert grounding.verify_quote("原文在这里", "原文")
    assert not grounding.verify_quote("原文在这里", "这句不存在")


def test_evidence_jumpable_by_source():
    assert grounding._jumpable("book", {"char_start": 0, "char_end": 10}, "i1") is True
    # 没有字符区间但有 item_id：仍可打开条目详情
    assert grounding._jumpable("book", {}, "i1") is True
    assert grounding._jumpable("weread", {"deep_link": "https://x"}, "i1") is True
    assert grounding._jumpable("weread", {}, "") is False
    assert grounding._jumpable("manual", {}, "i1") is True


# ---- build_evidence 三级判定 --------------------------------------------------


def _seed(session, *, title, content, hex_id=None, embed_failed=False):
    item = _svc(session).add_knowledge(user_id="u1", title=title, content=content)
    if embed_failed:
        # 真实的 embed_failed 是「向量没算出来」，所以连向量也要清掉；
        # 只改状态不清向量，会让后面的相关性判定拿到一个其实存在的余弦。
        item.embed_status = "embed_failed"
        item.embedding = None
        session.commit()
    return item


def test_build_evidence_returns_citable_items(session):
    _seed(session, title="数据库索引", content="B+树适合范围查询，哈希索引只适合等值查询。")
    items = grounding.build_evidence(
        session,
        user_id="u1",
        query="B+树 范围查询",
        min_relevance=0.0,
        embedding=HashEmbedding(),
    )
    assert items, "低阈值下应能召回"
    ev = items[0]
    assert ev.kind == "knowledge_item"
    assert ev.basis == grounding.BASIS_LIBRARY
    assert ev.can_cite is True
    assert ev.quote in _content_of(session, ev.item_id)


def test_build_evidence_empty_query_returns_nothing(session):
    _seed(session, title="标题", content="内容")
    assert grounding.build_evidence(session, user_id="u1", query="  ", embedding=HashEmbedding()) == []


def test_build_evidence_high_threshold_filters_out(session):
    _seed(session, title="番茄炒蛋", content="三个番茄两个蛋，先炒蛋再炒番茄。")
    items = grounding.build_evidence(
        session,
        user_id="u1",
        query="B+树 索引 查询优化",
        min_relevance=0.99,
        embedding=HashEmbedding(),
    )
    assert items == []


def test_build_evidence_marks_keyword_retrieval_when_embed_failed(session):
    _seed(session, title="索引结构", content="B+树适合范围查询。", embed_failed=True)
    items = grounding.build_evidence(
        session,
        user_id="u1",
        query="B+树范围查询",
        min_relevance=0.0,
        embedding=HashEmbedding(),
    )
    assert items
    assert items[0].retrieval == "keyword"
    assert items[0].relevance <= grounding.KEYWORD_RELEVANCE_CAP


def test_build_evidence_excludes_soft_deleted(session):
    item = _seed(session, title="待删条目", content="B+树适合范围查询。")
    IngestionService(session, HashEmbedding()).delete_knowledge(user_id="u1", item_id=item.id)
    items = grounding.build_evidence(
        session, user_id="u1", query="B+树范围查询", min_relevance=0.0, embedding=HashEmbedding()
    )
    assert all(e.item_id != item.id for e in items)


def test_build_evidence_user_scoped(session):
    _seed(session, title="甲的条目", content="B+树适合范围查询。")
    items = grounding.build_evidence(
        session, user_id="u2", query="B+树范围查询", min_relevance=0.0, embedding=HashEmbedding()
    )
    assert items == []


def _content_of(session, item_id: str) -> str:
    item = KnowledgeRepository(session, user_id="u1").get(item_id)
    assert item is not None
    return item.raw_content


# ---- AT-A-06：需 2 条以上才限流 ------------------------------------------------


def test_dedupe_by_book_limits_per_book():
    ev = [
        grounding.Evidence(
            evidence_id=f"ki:{i}", kind="knowledge_item", basis=grounding.BASIS_LIBRARY,
            source="book", title="同一本书", quote="片段",
            locator={"book_id": "b1"}, relevance=0.9 - i * 0.01, item_id=str(i), level=grounding.LEVEL_L3,
        )
        for i in range(5)
    ]
    kept = grounding.dedupe_by_book(ev, max_per_book=2)
    assert len(kept) == 2


def test_dedupe_does_not_limit_manual_items():
    """手记没有"书"的归属，不能按标题归并限流。

    回归：原实现用 `locator.book_id or title` 作 key，两条都叫「读书笔记」的
    独立手记会被当成同一本书裁掉一条——少给依据比多给更难发现。
    """
    ev = [
        grounding.Evidence(
            evidence_id=f"ki:{i}", kind="knowledge_item", basis=grounding.BASIS_LIBRARY,
            source="manual", title="读书笔记", quote="片段",
            locator={}, relevance=0.8, item_id=str(i), level=grounding.LEVEL_L3,
        )
        for i in range(4)
    ]
    assert len(grounding.dedupe_by_book(ev, max_per_book=2)) == 4


def test_relevance_threshold_matches_real_embedding_scale():
    """召回阈值必须低于真实模型对相关文本的打分。

    回归：默认值曾是 0.55，而本机 ONNX 模型对"直接相关"只给 0.509、
    "同书相关" 0.490——B 模块在真实数据上永远报"依据不足"，功能看着做了其实用不了。
    Mock 用的 HashEmbedding 分数分布完全不同，所以这条只能靠常量边界钉住。
    """
    from app.agent.grounding import DEFAULT_MIN_RELEVANCE

    assert DEFAULT_MIN_RELEVANCE <= 0.5, "再高就会滤掉真实模型判为相关的内容"
    assert DEFAULT_MIN_RELEVANCE >= 0.3, "再低会把无关内容（实测 0.27）也放进来"


def test_keyword_zero_overlap_filtered_out(session):
    """完全没有词面重叠的条目不能进证据池。

    回归：原实现 keyword 路径不过任何门槛，语义检索失败时几乎任何条目都能
    变成"依据"，"依据不足"的判定随之失效。
    """
    item = _seed(session, title="番茄炒蛋", content="三个番茄两个蛋，先炒蛋再炒番茄。", embed_failed=True)
    items = grounding.build_evidence(
        session, user_id="u1", query="B+树 索引 查询优化",
        min_relevance=0.0, embedding=HashEmbedding(),
    )
    assert all(e.item_id != item.id for e in items)


# ---- 引用校验 ---------------------------------------------------------------


def _ev(eid: str, *, basis=grounding.BASIS_LIBRARY, level=grounding.LEVEL_L3) -> grounding.Evidence:
    return grounding.Evidence(
        evidence_id=eid, kind="knowledge_item", basis=basis, source="manual",
        title="t", quote="q", locator={}, relevance=0.8, item_id=eid, level=level,
    )


def test_verify_citations_keeps_valid_items():
    pool = [_ev("ki:1"), _ev("ki:2")]
    items = [{"action": "a", "basis_item_ids": ["ki:1", "ki:2"]}]
    kept, dropped = grounding.verify_citations(items, pool)
    assert len(kept) == 1 and dropped == 0


def test_verify_citations_drops_fabricated_ids():
    """模型编了一个不存在的 id → 整条丢弃（不是删 id 留文案）。"""
    pool = [_ev("ki:1")]
    items = [{"action": "a", "basis_item_ids": ["ki:不存在的"]}]
    kept, dropped = grounding.verify_citations(items, pool)
    assert kept == [] and dropped == 1


def test_verify_citations_drops_empty_basis():
    kept, dropped = grounding.verify_citations([{"action": "a", "basis_item_ids": []}], [_ev("ki:1")])
    assert kept == [] and dropped == 1


def test_verify_citations_drops_l2_evidence():
    """l2（可召回不可溯源）不能作为依据。"""
    pool = [_ev("ki:1", level=grounding.LEVEL_L2)]
    kept, dropped = grounding.verify_citations([{"action": "a", "basis_item_ids": ["ki:1"]}], pool)
    assert kept == [] and dropped == 1


def test_verify_citations_rejects_agent_only_basis():
    """通读笔记不得单独构成依据：每条建议至少 1 条 library 级。"""
    pool = [_ev("bs:1", basis=grounding.BASIS_AGENT)]
    kept, dropped = grounding.verify_citations([{"action": "a", "basis_item_ids": ["bs:1"]}], pool)
    assert kept == [] and dropped == 1


def test_verify_citations_accepts_agent_plus_library():
    pool = [_ev("ki:1"), _ev("bs:1", basis=grounding.BASIS_AGENT)]
    kept, _ = grounding.verify_citations([{"action": "a", "basis_item_ids": ["ki:1", "bs:1"]}], pool)
    assert len(kept) == 1


# ---- 置信度 ------------------------------------------------------------------


def test_confidence_capped_when_slots_incomplete():
    full = grounding.confidence_for(
        basis_count=3, mean_relevance=1.0, slots_complete=True, dropped=0
    )
    partial = grounding.confidence_for(
        basis_count=3, mean_relevance=1.0, slots_complete=False, dropped=0
    )
    assert full > partial
    assert partial <= 0.6


def test_confidence_penalised_by_dropped_items():
    clean = grounding.confidence_for(
        basis_count=2, mean_relevance=0.8, slots_complete=True, dropped=0
    )
    dirty = grounding.confidence_for(
        basis_count=2, mean_relevance=0.8, slots_complete=True, dropped=1
    )
    assert clean > dirty


def test_confidence_in_unit_range():
    assert 0.0 <= grounding.confidence_for(
        basis_count=0, mean_relevance=0.0, slots_complete=False, dropped=3
    ) <= 1.0


# ---- 卡片载荷 ---------------------------------------------------------------


def test_evidence_card_payload_shape():
    ev = _ev("ki:1")
    card = ev.as_card()
    for key in (
        "evidence_id", "kind", "basis", "source", "title",
        "quote", "locator", "retrieval", "relevance", "jumpable", "level",
    ):
        assert key in card
    assert card["basis"] == grounding.BASIS_LIBRARY
