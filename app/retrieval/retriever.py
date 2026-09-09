"""检索编排（D2）：向量语义 + 关键词兜底双通道召回。

- 向量为主通道；embedding 模型不可用或条目未向量化时，关键词兜底顶替。
- 合并策略：union 后按通道归一化分数融权（alpha 加权），排序取 top_k。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.domain.models.knowledge_item import KnowledgeItem
from app.retrieval.embedding import EmbeddingModel
from app.retrieval.keyword import score_keyword
from app.retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class RetrievedItem:
    item_id: str
    score: float
    channels: list[str]


class Retriever:
    def __init__(self, embedding: EmbeddingModel, vector_store: VectorStore | None = None, vec_weight: float = 0.7) -> None:
        self._embedding = embedding
        self._vector_store = vector_store or VectorStore()
        self._vec_weight = vec_weight

    def _vector_scores(self, query, items, top_k, min_score):
        vec_candidates = [(i.id, self._embedding.loads(i.embedding)) for i in items if i.embedding]
        if not vec_candidates:
            return {}
        try:
            query_vec = self._embedding.embed([query])[0]
        except Exception as exc:
            logger.warning("embedding unavailable, fallback to keyword only: %s", exc)
            return {}
        scores = dict(self._vector_store.search(vec_candidates, query_vec, top_k=top_k, min_score=min_score))
        top = max(scores.values(), default=0.0) or 1.0
        return {k: v / top for k, v in scores.items()}

    @staticmethod
    def _keyword_scores(query, items):
        scores = {i.id: score_keyword(query, i.title, i.raw_content) for i in items}
        scores = {k: v for k, v in scores.items() if v > 0}
        top = max(scores.values(), default=1.0) or 1.0
        return {k: v / top for k, v in scores.items()}

    def retrieve(self, query, items, top_k=5, vec_min_score=0.05):
        vec_scores = self._vector_scores(query, items, top_k=top_k, min_score=vec_min_score)
        kw_scores = self._keyword_scores(query, items)

        merged = {}
        for iid in set(vec_scores) | set(kw_scores):
            v = vec_scores.get(iid, 0.0)
            k = kw_scores.get(iid, 0.0)
            has_v, has_k = iid in vec_scores, iid in kw_scores
            if has_v and has_k:
                score = self._vec_weight * v + (1 - self._vec_weight) * k
            elif has_v:
                score = v
            else:
                score = k * 0.1
            merged[iid] = (score, has_v, has_k)

        ranked = sorted(merged.items(), key=lambda kv: kv[1][0], reverse=True)[:top_k]
        return [
            RetrievedItem(
                item_id=iid,
                score=info[0],
                channels=(["vector"] if info[1] else []) + (["keyword"] if info[2] else []),
            )
            for iid, info in ranked
        ]