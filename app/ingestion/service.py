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
        if self._embedding is not None:
            self._try_embed(item)
        return item

    def _try_embed(self, item: KnowledgeItem) -> None:
        try:
            vec = self._embedding.embed([item.title + "\n" + item.raw_content])[0]
            item.embedding = EmbeddingModel.dumps(vec)
            item.embed_status = "embedded"
        except Exception as exc:  # 降级：不阻断主链路
            item.embed_status = "embed_failed"
            logger.warning("embed failed item=%s: %s", item.id, exc)