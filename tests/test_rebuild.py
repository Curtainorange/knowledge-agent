"""嵌入向量增量重建测试：模型版本记录 → 陈旧识别 → 选择性重建。"""
from __future__ import annotations

from app.domain.models.knowledge_item import KnowledgeItem
from app.ingestion.service import IngestionService
from app.retrieval.embedding import HashEmbedding
from app.retrieval.rebuild import find_stale_items, rebuild_stale_embeddings
from app.retrieval.vector_store import InMemoryVectorStore


class FakeModelV2(HashEmbedding):
    """模拟「换了一个嵌入模型」：name 不同，向量维度相同（重算可比性由 name 判断）。"""

    @property
    def name(self) -> str:
        return "fake-model-v2"


def _seed(session, n: int = 3) -> list[KnowledgeItem]:
    svc = IngestionService(session, HashEmbedding())
    items = [
        svc.add_knowledge(user_id="u1", title=f"条目{i}", content=f"内容{i}")
        for i in range(n)
    ]
    return items


def test_ingestion_records_embedding_meta(session):
    item = _seed(session, 1)[0]
    assert item.embedding_model == "hash"
    assert item.embedding_dim == HashEmbedding.dim
    assert item.embed_status == "embedded"


def test_stale_detection_by_model_name(session):
    items = _seed(session, 2)
    emb = HashEmbedding()
    # 当前模型 hash → 无陈旧
    assert find_stale_items(session, emb.name) == []
    # 换模型后 → 全部陈旧
    stale = find_stale_items(session, "fake-model-v2")
    assert {i.id for i in stale} == {i.id for i in items}


def test_stale_detection_includes_never_embedded(session):
    item = _seed(session, 1)[0]
    # 模拟历史条目：有向量但没有模型记录（0012 迁移前的存量数据）
    item.embedding_model = None
    session.commit()
    stale = find_stale_items(session, "hash")
    assert [i.id for i in stale] == [item.id]


def test_rebuild_recomputes_stale_and_upserts_store(session):
    items = _seed(session, 2)
    emb_v2 = FakeModelV2()
    store = InMemoryVectorStore()
    stats = rebuild_stale_embeddings(session, embedding=emb_v2, store=store)
    assert stats["total"] == 2
    assert stats["stale"] == 2
    assert stats["rebuilt"] == 2
    assert stats["failed"] == 0
    for item in items:
        session.refresh(item)
        assert item.embedding_model == "fake-model-v2"
        assert item.embed_status == "embedded"
    # 重建后向量索引同步更新（条目可经新向量召回）
    assert store.count() == 2
    hits = store.keyword_search("条目1", user_id="u1", top_k=2)
    target = next(i for i in items if i.title == "条目1")
    assert hits and hits[0][0] == target.id


def test_rebuild_failure_keeps_item_untouched(session, monkeypatch):
    item = _seed(session, 1)[0]
    old_embedding = item.embedding
    emb_v2 = FakeModelV2()

    def broken_embed(self, texts):
        raise RuntimeError("模型加载失败")

    monkeypatch.setattr(FakeModelV2, "embed", broken_embed)
    stats = rebuild_stale_embeddings(session, embedding=emb_v2, store=InMemoryVectorStore())
    assert stats["failed"] == 1 and stats["rebuilt"] == 0
    session.refresh(item)
    # 失败条目保留原状（旧向量不丢，检索仍可用）
    assert item.embedding == old_embedding
    assert item.embedding_model == "hash"


def test_rebuild_up_to_date_is_noop(session):
    _seed(session, 2)
    emb = HashEmbedding()
    store = InMemoryVectorStore()
    stats = rebuild_stale_embeddings(session, embedding=emb, store=store)
    assert stats["stale"] == 0 and stats["rebuilt"] == 0


def test_rebuild_limit_and_batch_commit(session):
    _seed(session, 5)
    emb_v2 = FakeModelV2()
    stats = rebuild_stale_embeddings(
        session, embedding=emb_v2, store=InMemoryVectorStore(), limit=3, batch_size=2
    )
    assert stats["stale"] == 3 and stats["rebuilt"] == 3
