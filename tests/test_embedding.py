"""Retrieval embedding 测试：hash 后端确定性/维度/归一化，dumps/loads 往返。"""
from __future__ import annotations

import numpy as np
import pytest

from app.core.config import settings
from app.retrieval.embedding import EmbeddingModel, HashEmbedding, build_embedding


def test_conftest_forces_hash_backend():
    assert settings.embedding_backend == "hash"


def test_hash_is_deterministic():
    m = HashEmbedding()
    v1 = m.embed(["知识管理 系统设计"])
    v2 = m.embed(["知识管理 系统设计"])
    assert v1 == v2


def test_hash_dim_and_normalized():
    m = HashEmbedding()
    v = m.embed(["认知副驾 收藏 碎片"])[0]
    assert len(v) == m.dim == 256
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-6


def test_hash_distinguishes_distinct_texts():
    m = HashEmbedding()
    a = m.embed(["数据库索引原理"])
    b = m.embed(["如何做番茄炒蛋"])
    from app.retrieval.vector_store import cos_sim
    assert cos_sim(a[0], b[0]) < 0.5


def test_dumps_loads_roundtrip():
    m = HashEmbedding()
    vec = m.embed(["测试向量"])[0]
    data = EmbeddingModel.dumps(vec)
    restored = EmbeddingModel.loads(data)
    assert restored == vec


def test_factory():
    assert isinstance(build_embedding("hash"), HashEmbedding)
    assert isinstance(build_embedding(), HashEmbedding)  # conftest 强制 hash


def test_factory_reuses_instance():
    """同后端必须复用实例：模型的加载状态（尤其是失败冷却）挂在实例上，
    每次新建会让冷却失效。"""
    assert build_embedding("hash") is build_embedding("hash")


def test_factory_rejects_unknown_backend():
    with pytest.raises(ValueError):
        build_embedding("no-such-backend")


def test_local_backend_reports_missing_model_clearly(tmp_path):
    """local 后端在模型缺失时，错误信息要能直接指导用户怎么补。"""
    from app.retrieval.embedding import LocalOnnxEmbedding

    model = LocalOnnxEmbedding(model_dir=str(tmp_path / "not-there"))
    with pytest.raises(RuntimeError) as excinfo:
        model.embed(["任意文本"])
    assert "fetch_embedding_model" in str(excinfo.value)


def test_local_backend_lazy_loads():
    """构造时不应加载模型（延迟到首次 embed），否则服务启动就会被拖慢。"""
    from app.retrieval.embedding import LocalOnnxEmbedding

    model = LocalOnnxEmbedding(model_dir="definitely/not/a/real/dir")
    assert model._session is None