"""检索编排（D2）：向量语义 + 关键词双通道召回 + 融合排序。

- 双通道：向量为主通道；embedding 模型不可用或条目未向量化时，关键词兜底顶替（可靠-4）。
- 库内检索：`retrieve` 不传全量条目列表，只传过滤条件（user_id），
  由 `VectorStore.search` / `keyword_search` 在库内返回 top-k 命中（id + 分数）。
- 融合（fusion 参数二选一）：
  - "rrf"（默认）：Reciprocal Rank Fusion，按**排名**融合
    score = w_vec/(k+rank_vec) + w_kw/(k+rank_kw)。对两路分数尺度不敏感，
    单通道命中的惩罚由公式自然给出（缺一路就少加一项），无需拍常数。
    旧实现按「分数归一化加权」融合：向量余弦挤在 0.9+ 高分区而关键词得分
    拉得开，归一化后两路有效权重经常偏离设定值；单通道命中还要再乘 0.1
    的硬编码惩罚。RRF 消掉了这两个任意常数（借鉴 agentic-local-brain）。
  - "weighted"：旧的归一化加权融合，保留作对照 / 回归用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.retrieval.embedding import EmbeddingModel
from app.retrieval.vector_store import VectorStore, build_vector_store

logger = logging.getLogger(__name__)

FUSION_RRF = "rrf"
FUSION_WEIGHTED = "weighted"


@dataclass
class RetrievedItem:
    item_id: str
    score: float
    channels: list[str]


class Retriever:
    def __init__(
        self,
        embedding: EmbeddingModel,
        vector_store: VectorStore | None = None,
        vec_weight: float = 0.7,
        fusion: str = FUSION_RRF,
        rrf_k: int = 60,
    ) -> None:
        self._embedding = embedding
        # 默认走工厂单例（与写入路径 upsert 的是同一个索引）
        self._vector_store = vector_store or build_vector_store()
        self._vec_weight = vec_weight
        if fusion not in (FUSION_RRF, FUSION_WEIGHTED):
            raise ValueError(f"未知融合策略: {fusion!r}（可选 {FUSION_RRF} / {FUSION_WEIGHTED}）")
        self._fusion = fusion
        self._rrf_k = rrf_k

    # ---- 单通道召回：[(id, 原始分数)] 按分数降序，两个融合策略共用同一次查询 --

    def _vector_channel(self, query, user_id, top_k, min_score) -> list[tuple[str, float]]:
        try:
            query_vec = self._embedding.embed([query])[0]
        except Exception as exc:
            logger.warning("embedding unavailable, fallback to keyword only: %s", exc)
            return []
        try:
            hits = self._vector_store.search(
                query_vec, user_id=user_id, top_k=top_k, min_score=min_score
            )
        except Exception as exc:
            logger.warning("vector store search failed, fallback to keyword only: %s", exc)
            return []
        return [(iid, float(score)) for iid, score in hits]

    def _keyword_channel(self, query, user_id, top_k) -> list[tuple[str, float]]:
        try:
            hits = self._vector_store.keyword_search(query, user_id=user_id, top_k=top_k)
        except Exception as exc:
            logger.warning("keyword search failed: %s", exc)
            return []
        return [(iid, float(score)) for iid, score in hits]

    # ---- 融合 ----------------------------------------------------------------

    @staticmethod
    def _normalize(hits: list[tuple[str, float]]) -> dict[str, float]:
        """原始分数按通道内最大值归一化到 0..1（旧融合用）。"""
        scores = dict(hits)
        top = max(scores.values(), default=0.0) or 1.0
        return {iid: s / top for iid, s in scores.items()}

    def _fuse(
        self,
        vec_hits: list[tuple[str, float]],
        kw_hits: list[tuple[str, float]],
    ) -> dict[str, tuple[float, bool, bool]]:
        """融合两路召回，返回 {id: (score, 是否向量命中, 是否关键词命中)}。"""
        has_v_ids = {iid for iid, _ in vec_hits}
        has_k_ids = {iid for iid, _ in kw_hits}

        if self._fusion == FUSION_RRF:
            # rank 从 1 计；只出现在单一路里的 id 自然只加一项，无需人为惩罚系数
            w_vec, w_kw = self._vec_weight, 1 - self._vec_weight
            fused: dict[str, float] = {}
            for rank, (iid, _) in enumerate(vec_hits, start=1):
                fused[iid] = fused.get(iid, 0.0) + w_vec / (self._rrf_k + rank)
            for rank, (iid, _) in enumerate(kw_hits, start=1):
                fused[iid] = fused.get(iid, 0.0) + w_kw / (self._rrf_k + rank)
        else:
            vec_scores = self._normalize(vec_hits)
            kw_scores = self._normalize(kw_hits)
            fused = {}
            for iid in set(vec_scores) | set(kw_scores):
                v = vec_scores.get(iid, 0.0)
                k = kw_scores.get(iid, 0.0)
                if iid in vec_scores and iid in kw_scores:
                    fused[iid] = self._vec_weight * v + (1 - self._vec_weight) * k
                elif iid in vec_scores:
                    fused[iid] = v
                else:
                    fused[iid] = k * 0.1  # 仅关键词命中的降权（历史行为，保留对照）

        return {
            iid: (score, iid in has_v_ids, iid in has_k_ids) for iid, score in fused.items()
        }

    def retrieve(self, query, *, user_id=None, top_k=5, vec_min_score=0.05):
        """双通道召回：只传过滤条件（user_id），由 store 返回 top-k + id。

        embedding 失败或向量库不可用时自动降级为关键词兜底，不抛异常（可靠-4）。
        """
        vec_hits = self._vector_channel(query, user_id, top_k=top_k, min_score=vec_min_score)
        kw_hits = self._keyword_channel(query, user_id, top_k=top_k)

        merged = self._fuse(vec_hits, kw_hits)
        ranked = sorted(merged.items(), key=lambda kv: kv[1][0], reverse=True)[:top_k]
        return [
            RetrievedItem(
                item_id=iid,
                score=info[0],
                channels=(["vector"] if info[1] else []) + (["keyword"] if info[2] else []),
            )
            for iid, info in ranked
        ]
