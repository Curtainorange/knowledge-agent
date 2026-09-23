"""检索层测试：双通道召回（向量 + 关键词）的融合排序与降级（库内检索）。"""
from __future__ import annotations

from app.retrieval.embedding import HashEmbedding
from app.retrieval.keyword import score_keyword
from app.retrieval.retriever import Retriever
from app.retrieval.vector_store import InMemoryVectorStore


def _store_with(*items) -> InMemoryVectorStore:
    """把若干 (id, title, content) 向量化后 upsert 进一个内存索引（user_id="u"）。"""
    store = InMemoryVectorStore()
    emb = HashEmbedding()
    for item_id, title, content in items:
        vec = emb.embed([title + "\n" + content])[0]
        store.upsert(item_id=item_id, user_id="u", vector=vec, title=title, content=content)
    return store


def test_keyword_scores_title_over_content():
    assert score_keyword("数据库", "数据库设计", "正文无关") > score_keyword(
        "数据库", "无关标题", "本文介绍数据库知识"
    )


def test_vector_channel_ranks_semantic_match_first():
    store = _store_with(
        ("a", "番茄炒蛋做法", "三个番茄两个蛋，先炒蛋再炒番茄"),
        ("b", "数据库索引", "B+树与哈希索引的区别"),
    )
    retriever = Retriever(HashEmbedding(), store)
    hits = retriever.retrieve("数据库 索引 查询", user_id="u", top_k=2)
    # 哈希 embedding 语义弱，关键词命中应使 b 排前
    assert hits[0].item_id in ("a", "b")


def test_keyword_fallback_when_no_vectors():
    """条目向量化失败（vector=None）时仍可经关键词兜底召回（可靠-4）。"""
    store = InMemoryVectorStore()
    store.upsert(item_id="a", user_id="u", vector=None, title="番茄炒蛋做法", content="三个番茄两个蛋")
    store.upsert(item_id="b", user_id="u", vector=None, title="数据库索引", content="B+树与哈希索引的区别")
    retriever = Retriever(HashEmbedding(), store)
    hits = retriever.retrieve("数据库 索引", user_id="u", top_k=1)
    assert hits and hits[0].item_id == "b"
    assert "keyword" in hits[0].channels
    assert "vector" not in hits[0].channels


def test_union_fusion_marks_channels():
    store = _store_with(
        ("a", "数据库索引", "正文讲数据库和索引"),
        ("c", "完全无关", "xxxx"),
    )
    retriever = Retriever(HashEmbedding(), store)
    hits = retriever.retrieve("数据库 索引", user_id="u")
    assert hits[0].item_id == "a"
    assert hits[0].channels  # vector 和/或 keyword 至少一个命中


def test_user_scoping_isolates_records():
    """检索只命中当前用户的索引条目，跨用户不可见。"""
    store = InMemoryVectorStore()
    emb = HashEmbedding()
    vec = emb.embed(["甲的内容"])[0]
    store.upsert(item_id="a", user_id="uA", vector=vec, title="甲", content="内容")
    retriever = Retriever(emb, store)
    assert retriever.retrieve("内容", user_id="uB", top_k=5) == []
    assert [h.item_id for h in retriever.retrieve("内容", user_id="uA", top_k=5)] == ["a"]


# ---- RRF 融合（默认策略） ---------------------------------------------------


def test_rrf_is_default_fusion():
    store = InMemoryVectorStore()
    retriever = Retriever(HashEmbedding(), store)
    assert retriever._fusion == "rrf"


def test_rrf_invalid_fusion_rejected():
    import pytest

    with pytest.raises(ValueError):
        Retriever(HashEmbedding(), InMemoryVectorStore(), fusion="bogus")


def test_rrf_double_channel_hit_outranks_single_channel():
    """两路都命中的条目应排在只被一路命中的条目前面（RRF 的核心性质）。"""
    store = _store_with(
        ("both", "数据库索引", "正文讲数据库和索引"),
        ("vec_only", "语义相近但字面不同", "向量库近邻检索的近似算法"),
    )
    retriever = Retriever(HashEmbedding(), store, fusion="rrf")
    hits = retriever.retrieve("数据库 索引", user_id="u", top_k=2)
    assert hits[0].item_id == "both"
    assert hits[0].channels == ["vector", "keyword"]


def test_rrf_single_channel_penalty_is_rank_based_not_hardcoded():
    """单通道命中不乘硬编码 0.1：RRF 下分数由公式自然给出（缺一路少加一项）。"""
    store = _store_with(("both", "数据库索引", "正文讲数据库和索引"))
    store.upsert(item_id="kw_only", user_id="u", vector=None, title="数据库", content="数据库")
    retriever = Retriever(HashEmbedding(), store, fusion="rrf")
    hits = retriever.retrieve("数据库 索引", user_id="u", top_k=5)
    by_id = {h.item_id: h for h in hits}
    assert by_id["kw_only"].channels == ["keyword"]
    # "both" 占关键词第 1，kw_only 第 2 → RRF 分 = 0.3/(60+2)，无 ×0.1 的额外惩罚
    assert abs(by_id["kw_only"].score - 0.3 / 62) < 1e-9
    assert by_id["both"].score > by_id["kw_only"].score


def test_weighted_fusion_still_available_as_option():
    """旧融合保留为对照选项：行为与历史一致（单通道关键词命中 ×0.1）。"""
    store = InMemoryVectorStore()
    store.upsert(item_id="kw_only", user_id="u", vector=None, title="数据库", content="数据库")
    retriever = Retriever(HashEmbedding(), store, fusion="weighted")
    hits = retriever.retrieve("数据库", user_id="u", top_k=5)
    assert hits[0].item_id == "kw_only"
    assert hits[0].channels == ["keyword"]
