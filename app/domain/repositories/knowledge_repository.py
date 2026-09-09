"""知识条目仓储：当前用户的录入与检索读取，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.base import BaseRepository


class KnowledgeRepository(BaseRepository[KnowledgeItem]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self, *, user_id: str, title: str, content: str, source: str = "manual", tags: list | None = None
    ) -> KnowledgeItem:
        item = KnowledgeItem(
            user_id=user_id,
            title=title,
            raw_content=content,
            source=source,
            tags=tags or [],
            read_progress=0.0,
            embed_status="pending",
        )
        self._session.add(item)
        self._session.flush()
        return item

    def list_active(self, user_id: str) -> list[KnowledgeItem]:
        """当前用户所有未删除条目（用于检索）。"""
        self._guard(user_id)
        stmt = (
            select(KnowledgeItem)
            .where(KnowledgeItem.user_id == user_id, KnowledgeItem.is_deleted.is_(False))
            .order_by(KnowledgeItem.created_at.desc())
        )
        return list(self._session.scalars(stmt))

    def get(self, item_id: str) -> KnowledgeItem | None:
        item = self._session.get(KnowledgeItem, item_id)
        if item is None:
            return None
        self._guard(item.user_id)
        return item