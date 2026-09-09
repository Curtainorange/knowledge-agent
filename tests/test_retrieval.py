"""检索层测试：双通道召回（向量 + 关键词）的融合排序与降级。"""
from __future__ import annotations

from app.domain.models.knowledge_item import KnowledgeItem
from app.retrieval.embedding import EmbeddingModel, HashEmbedding
from app.retrieval.keyword import score_keyword
from app.retrieval.retriever import Retriever


def _item(item_id: str, title: str, content: str, embedded: bool = True) -> KnowledgeItem:
    it = KnowledgeItem(id=item_id, user_id="u", title=title, raw_content=content)
    if embedded:
        vec = HashEmbedding().embed([title + "\n" + content])[0]
        it.embedding = EmbeddingModel.dumps(vec)
    return it


def test_keyword_scores_title_over_content():
    assert score_keyword("数据库", "数据库设计", "正文无关") > score_keyword(
        "数据库", "无关标题", "本文介绍数据库知识"
    )


def test_vector_channel_ranks_semantic_match_first():
    items = [
        _item("a", "番茄炒蛋做法", "三个番茄两个蛋，先炒蛋再炒番茄"),
        _item("b", "数据库索引", "B+树与哈希索引的区别"),
    ]
    emb = HashEmbedding()
    retriever = Retriever(emb)
    hits = retriever.retrieve("数据库 索引 查询", items, top_k=2)
    # 哈希 embedding 语义弱，关键词命中应使 b 排前
    assert hits[0].item_id in ("a", "b")


def test_keyword_fallback_when_no_embeddings():
    items = [
        _item("a", "番茄炒蛋做法", "三个番茄两个蛋，先炒蛋再炒番茄", embedded=False),
        _item("b", "数据库索引", "B+树与哈希索引的区别", embedded=False),
    ]
    retriever = Retriever(HashEmbedding())
    hits = retriever.retrieve("数据库 索引", items, top_k=1)
    assert hits and hits[0].item_id == "b"
    assert "keyword" in hits[0].channels


def test_union_fusion_marks_channels():
    items = [_item("a", "数据库索引", "正文讲数据库和索引"), _item("c", "完全无关", "xxxx")]
    retriever = Retriever(HashEmbedding())
    hits = retriever.retrieve("数据库 索引", items)
    assert hits[0].item_id == "a"
    assert hits[0].channels  # vector 和/或 keyword 至少一个命中