"""向量库抽象与默认内存实现（库内检索，不再「全量传入再筛」）。

- `VectorStore`：统一接口 `search`（向量）/ `keyword_search`（兜底）/ `upsert` / `delete`。
- `InMemoryVectorStore`：P0 默认进程内索引；写入路径同步 upsert，读取路径库内检索，
  冷启动时经 loader 从关系库的 `embedding` 列懒回填（保住「重启后仍可检索」的既有行为）。
- `build_vector_store`：按 `settings.vector_backend` 选择实现，同后端复用单例
  （与 `build_embedding` 同款理由：实现上的加载/回填状态应只存在一份）。
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Sequence

from app.core.config import settings
from app.retrieval.keyword import score_keyword

logger = logging.getLogger(__name__)


def cos_sim(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度（两向量均已归一化时可忽略分母做点积加速）。"""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(y * y for y in b)) or 1e-9
    return dot / (na * nb)


@dataclass
class VectorRecord:
    """库内一条可检索记录：向量 + 关键词兜底所需的文本 + 归属作用域。

    `vector` 可为 None：条目向量化失败时仍写入索引（仅带文本），
    保证「embedding 失败 → 关键词兜底」的降级链不丢条目（可靠-4）。
    """

    item_id: str
    user_id: str
    title: str = ""
    content: str = ""
    vector: list[float] | None = None


class VectorStore(ABC):
    """向量库抽象：按查询向量返回 top-k 命中的 item_id 与相似度（库内检索）。

    检索侧不再接收全量条目列表——只传过滤条件（user_id），由具体后端在库内完成
    相似度计算与排序；写入侧经 upsert/delete 保持索引与事实来源同步。
    """

    @abstractmethod
    def upsert(
        self,
        *,
        item_id: str,
        user_id: str,
        vector: list[float] | None = None,
        title: str = "",
        content: str = "",
    ) -> None:
        """写入 / 覆盖一条记录。vector 为 None 时仅索引文本（关键词兜底）。"""

    @abstractmethod
    def delete(self, *, item_id: str, user_id: str) -> None:
        """删除一条记录（软删时同步调用，避免已删条目继续被召回）。"""

    @abstractmethod
    def search(
        self,
        query_vec: list[float],
        *,
        user_id: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        """向量近邻检索：返回 [(item_id, 相似度), ...]，按相似度降序、过滤阈值、截断 top_k。"""

    @abstractmethod
    def keyword_search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """关键词兜底检索（可靠-4）：返回 [(item_id, 得分), ...]，按得分降序。"""

    def count(self) -> int | None:
        """索引内记录总数（doctor 一致性检查用）。后端不支持时返回 None。"""
        return None


def _default_backfill_loader() -> list[VectorRecord]:
    """冷启动回填：从关系库读出已持久化的条目向量（未删除且已向量化）。

    惰性导入 db / 模型，避免 `retrieval` 在模块加载期反向依赖领域层。
    任何一条损坏（pickle 反序列化失败）只跳过该条，不拖垮整次回填。
    """
    from sqlalchemy import select

    from app.domain.db import SessionLocal
    from app.domain.models.knowledge_item import KnowledgeItem
    from app.retrieval.embedding import EmbeddingModel

    records: list[VectorRecord] = []
    session = SessionLocal()
    try:
        rows = session.scalars(
            select(KnowledgeItem).where(KnowledgeItem.is_deleted.is_(False))
        ).all()
        for row in rows:
            vec: list[float] | None = None
            if row.embedding:
                try:
                    vec = EmbeddingModel.loads(row.embedding)
                except Exception:  # noqa: BLE001 - 单条损坏不影响整体
                    vec = None
            records.append(
                VectorRecord(
                    item_id=row.id,
                    vector=vec,
                    user_id=row.user_id,
                    title=row.title,
                    content=row.raw_content,
                )
            )
    finally:
        session.close()
    return records


# 回填 loader 可由测试替换为 no-op（测试里事实来源是内存 SQLite，且经 upsert 写入）。
_backfill_loader: Callable[[], list[VectorRecord]] = _default_backfill_loader


def set_backfill_loader(loader: Callable[[], list[VectorRecord]] | None) -> None:
    """替换回填 loader（供测试注入 no-op，避免读到本机 dev.db）。"""
    global _backfill_loader
    _backfill_loader = loader or (lambda: [])


class InMemoryVectorStore(VectorStore):
    """进程内暴力余弦索引（P0 默认，单用户数百~低千条可接受，免外部向量库）。"""

    def __init__(self, loader: Callable[[], list[VectorRecord]] | None = None) -> None:
        self._records: dict[str, VectorRecord] = {}
        self._loaded = False
        self._loader = loader

    @staticmethod
    def _key(item_id: str, user_id: str) -> str:
        return f"{user_id}\x00{item_id}"

    def _ensure_loaded(self) -> None:
        """首次访问时懒回填（冷启动），之后直接用内存索引。"""
        if self._loaded:
            return
        self._loaded = True
        if self._loader is None:
            return
        try:
            for record in self._loader():
                self._records[self._key(record.item_id, record.user_id)] = record
        except Exception as exc:  # noqa: BLE001 - 回填失败降级为空索引，检索仍可用
            logger.warning("vector store backfill failed: %s", exc)

    def upsert(
        self,
        *,
        item_id: str,
        user_id: str,
        vector: list[float] | None = None,
        title: str = "",
        content: str = "",
    ) -> None:
        # 先回填再写入：进程重启后「第一条操作是录入」也能先补上历史条目
        self._ensure_loaded()
        self._records[self._key(item_id, user_id)] = VectorRecord(
            item_id=item_id,
            vector=list(vector) if vector is not None else None,
            user_id=user_id,
            title=title,
            content=content,
        )

    def delete(self, *, item_id: str, user_id: str) -> None:
        # 未加载时无需回填：软删条目本就不会被 loader 读回（loader 过滤 is_deleted）
        self._records.pop(self._key(item_id, user_id), None)

    def search(
        self,
        query_vec: list[float],
        *,
        user_id: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        self._ensure_loaded()
        scored: list[tuple[str, float]] = []
        for record in self._records.values():
            if user_id is not None and record.user_id != user_id:
                continue
            if record.vector is None:
                continue  # 未向量化条目只走关键词通道
            sim = cos_sim(query_vec, record.vector)
            if sim >= min_score:
                scored.append((record.item_id, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def keyword_search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        self._ensure_loaded()
        scored: list[tuple[str, float]] = []
        for record in self._records.values():
            if user_id is not None and record.user_id != user_id:
                continue
            s = score_keyword(query, record.title, record.content)
            if s > 0:
                scored.append((record.item_id, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def count(self) -> int:
        self._ensure_loaded()
        return len(self._records)


_instances: dict[str, VectorStore] = {}


def build_vector_store(backend: str | None = None) -> VectorStore:
    """按配置构造向量库实现（工厂）。

    同 backend 复用同一实例：内存后端的回填状态（已加载/未加载）挂在实例上，
    每次新建都会让冷启动回填失效、甚至与写入路径的 upsert 各写各的。
    """
    backend = backend or settings.vector_backend
    if backend not in _instances:
        if backend == "memory":
            _instances[backend] = InMemoryVectorStore(loader=_backfill_loader)
        elif backend == "pgvector":
            from app.retrieval.pgvector_store import PgvectorStore

            _instances[backend] = PgvectorStore()
        else:
            raise ValueError(f"未知 vector_backend: {backend!r}")
    return _instances[backend]


def reset_vector_store_cache() -> None:
    """清空实例缓存（仅供测试切换后端 / 隔离用例）。"""
    _instances.clear()
