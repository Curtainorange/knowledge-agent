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

logger = logging.getLogger(__name__)


class IngestionService:
    def __init__(self, session: Session, embedding: EmbeddingModel | None = None):
        self._session = session
        self._embedding = embedding

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
        return item

    def delete_knowledge(self, *, user_id: str, item_id: str) -> KnowledgeItem | None:
        """软删条目（保留原文，仅置 is_deleted）。不存在返回 None。"""
        repo = KnowledgeRepository(self._session, user_id=user_id)
        return repo.soft_delete(item_id)

    def _try_embed(self, item: KnowledgeItem) -> None:
        try:
            vec = self._embedding.embed([item.title + "\n" + item.raw_content])[0]
            item.embedding = EmbeddingModel.dumps(vec)
            item.embed_status = "embedded"
        except Exception as exc:  # 降级：不阻断主链路
            item.embed_status = "embed_failed"
            logger.warning("embed failed item=%s: %s", item.id, exc)