"""向量库测试：接口契约（search/keyword_search/upsert/delete）+ 工厂与 pgvector 惰性。"""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.retrieval.vector_store import (
    InMemoryVectorStore,
    VectorStore,
    build_vector_store,
    cos_sim,
)


def test_cos_sim_bounds():
    assert abs(cos_sim([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-9
    assert abs(cos_sim([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_inmemory_upsert_search_delete_roundtrip():
    store = InMemoryVectorStore()
    store.upsert(item_id="x", user_id="u", vector=[1.0, 0.0], title="数据库", content="索引原理")
    hits = store.search([1.0, 0.0], user_id="u", top_k=5, min_score=0.5)
    assert [h[0] for h in hits] == ["x"]
    assert hits[0][1] > 0.99

    store.delete(item_id="x", user_id="u")
    assert store.search([1.0, 0.0], user_id="u") == []


def test_inmemory_search_skips_unvectorized():
    store = InMemoryVectorStore()
    store.upsert(item_id="no-vec", user_id="u", vector=None, title="未向量化", content="正文")
    store.upsert(item_id="vec", user_id="u", vector=[1.0, 0.0], title="已向量化", content="正文")
    ids = [h[0] for h in store.search([1.0, 0.0], user_id="u")]
    assert ids == ["vec"]
    # 关键词通道仍能看到未向量化条目
    kw_ids = [h[0] for h in store.keyword_search("未向量化", user_id="u")]
    assert "no-vec" in kw_ids


def test_inmemory_user_scoping():
    store = InMemoryVectorStore()
    store.upsert(item_id="a", user_id="uA", vector=[1.0, 0.0], title="甲", content="内容")
    assert store.search([1.0, 0.0], user_id="uB") == []
    assert store.keyword_search("甲", user_id="uB") == []
    assert store.search([1.0, 0.0], user_id="uA")


def test_factory_memory_reuses_singleton():
    assert isinstance(build_vector_store("memory"), InMemoryVectorStore)
    assert build_vector_store("memory") is build_vector_store("memory")


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        build_vector_store("no-such-backend")


def test_factory_default_is_memory():
    assert isinstance(build_vector_store(), InMemoryVectorStore)


def test_pgvector_requires_dependency():
    """pgvector 未安装时，首次调用给出可直接行动的清晰错误，而不是连库才报。"""
    from app.retrieval.pgvector_store import PgvectorStore

    try:
        import pgvector  # noqa: F401
    except ImportError:
        store = PgvectorStore()
        with pytest.raises(RuntimeError, match="pgvector"):
            store.upsert(item_id="x", user_id="u", vector=[0.1, 0.2])
    else:  # pragma: no cover - 依赖已安装的环境跳过
        pytest.skip("pgvector 已安装，跳过缺依赖断言")


def test_pgvector_backend_config_selectable(monkeypatch):
    """vector_backend=pgvector 时工厂返回 PgvectorStore（惰性，构造不连库）。"""
    from app.retrieval.pgvector_store import PgvectorStore

    monkeypatch.setattr(settings, "vector_backend", "pgvector")
    from app.retrieval.vector_store import reset_vector_store_cache

    reset_vector_store_cache()
    assert isinstance(build_vector_store(), PgvectorStore)
    reset_vector_store_cache()
    monkeypatch.setattr(settings, "vector_backend", "memory")
