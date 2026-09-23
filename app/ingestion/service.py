"""知识接入最小服务（D4）：录入条目 + 计算 embedding。

顺序约定（对齐系统设计 §4.4）：先写关系库（事实来源），再算 embedding；
embedding 失败只影响召回（embed_status=embed_failed），不阻断条目落库（可靠-4）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.retrieval.embedding import EmbeddingModel
from app.retrieval.vector_store import VectorStore, build_vector_store

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(
        self,
        session: Session,
        embedding: EmbeddingModel | None = None,
        store: VectorStore | None = None,
    ):
        self._session = session
        self._embedding = embedding
        self._store = store  # None → 惰性取工厂单例（与检索侧共用同一索引）

    def _get_store(self) -> VectorStore:
        if self._store is None:
            self._store = build_vector_store()
        return self._store

    def add_knowledge(
        self, *, user_id: str, title: str, content: str, source: str = "manual", tags: list | None = None
    ) -> KnowledgeItem:
        repo = KnowledgeRepository(self._session, user_id=user_id)
        item = repo.create(user_id=user_id, title=title, content=content, source=source, tags=tags)

        # 先落库提交，再算向量。模型首次加载 / 下载可能耗时数十秒，若压在写事务里，
        # SQLite 的写锁会被长期占用，其它请求会直接撞上 "database is locked"。
        # 代价是顺序从「同事务写入」变为「先写事实来源、再补向量」——向量失败只影响
        # 召回质量，不影响条目存在（可靠-4），这个取舍是划算的。
        self._session.commit()

        if self._embedding is not None:
            self._try_embed(item)
            self._session.commit()

        # 主链路（录入）已完成，再排一次 L2 实时扫描——按小时桶合并，
        # 连续录入不会各触发一次；入队失败只记日志，不影响录入结果（见 triggers）。
        from app.workers.triggers import trigger_after_ingest

        trigger_after_ingest(self._session, user_id=user_id)

        # 行为埋点（L4/L5 的原材料）：只记元数据，正文不入事件表
        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.KNOWLEDGE_CREATED,
            payload={
                "item_id": item.id, "source": item.source,
                "title_len": len(item.title or ""), "content_len": len(item.raw_content or ""),
                "embed_status": item.embed_status,
            },
        )
        return item

    def update_knowledge(
        self,
        *,
        user_id: str,
        item_id: str,
        title: str | None = None,
        content: str | None = None,
        tags: list | None = None,
        read_progress: float | None = None,
    ) -> KnowledgeItem | None:
        """局部更新条目；文本变化才重算 embedding（避免无谓的向量开销）。

        条目不存在或不属于该用户 → 返回 None（不存在）/ 抛 PermissionError（越权）。
        """
        repo = KnowledgeRepository(self._session, user_id=user_id)
        item = repo.get(item_id)
        if item is None:
            return None
        text_changed = repo.apply_update(
            item, title=title, content=content, tags=tags, read_progress=read_progress
        )
        # 同 add_knowledge：先提交释放写锁，再补算向量
        self._session.commit()
        if text_changed and self._embedding is not None:
            self._try_embed(item)
            self._session.commit()

        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.KNOWLEDGE_UPDATED,
            payload={
                "item_id": item.id,
                "text_changed": text_changed,  # 是否触发重算向量（也是 L2 重扫的触发条件）
                "read_progress": item.read_progress,
            },
        )
        return item

    def delete_knowledge(self, *, user_id: str, item_id: str) -> KnowledgeItem | None:
        """软删条目（保留原文，仅置 is_deleted）。不存在返回 None。"""
        repo = KnowledgeRepository(self._session, user_id=user_id)
        item = repo.soft_delete(item_id)
        if item is not None:
            # 同步从向量库移除，避免已删条目继续被召回；失败不阻断软删（降级）
            try:
                self._get_store().delete(item_id=item_id, user_id=user_id)
            except Exception as exc:  # noqa: BLE001 - 索引一致性失败不阻断主链路
                logger.warning("vector store delete failed item=%s: %s", item_id, exc)

        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.KNOWLEDGE_DELETED,
            payload={"item_id": item_id, "found": item is not None},
        )
        return item

    def _try_embed(self, item: KnowledgeItem) -> None:
        vec: list[float] | None
        try:
            vec = self._embedding.embed([item.title + "\n" + item.raw_content])[0]
            item.embedding = EmbeddingModel.dumps(vec)
            item.embed_status = "embedded"
            # 记录模型标识与维度：换嵌入模型后据此增量重建（模型未知/不一致 → 重算）
            item.embedding_model = self._embedding.name
            item.embedding_dim = len(vec)
        except Exception as exc:  # 降级：不阻断主链路
            item.embed_status = "embed_failed"
            vec = None
            logger.warning("embed failed item=%s: %s", item.id, exc)

        # 无论向量化成功与否都同步写入向量库：成功带向量、失败仅带文本，
        # 保证「embedding 失败 → 关键词兜底」仍能召回该条目（可靠-4）。
        # 索引 upsert 失败只影响召回质量，不回滚已算好的向量。
        try:
            self._get_store().upsert(
                item_id=item.id,
                user_id=item.user_id,
                vector=vec,
                title=item.title,
                content=item.raw_content,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector store upsert failed item=%s: %s", item.id, exc)