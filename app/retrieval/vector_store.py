"""向量检索抽象（接口保留，P2 可切 pgvector/Milvus）。

MVP 用暴力余弦（单用户数百~低千条可接受，免向量库基础设施）。
items 以 [(item_id, vector), ...] 现场提供，便于从关系库按需加载。
"""
from __future__ import annotations

import math
from typing import Sequence


def cos_sim(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（两向量均已归一化时可忽略分母做点积加速）。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


class VectorStore:
    """向量库抽象：按查询向量返回 top-k 命中的 item_id 与相似度。"""

    def search(
        self,
        items: Sequence[tuple[str, list[float]]],
        query_vec: list[float],
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        scored = [(iid, cos_sim(query_vec, vec)) for iid, vec in items]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(iid, s) for iid, s in scored[:top_k] if s >= min_score]