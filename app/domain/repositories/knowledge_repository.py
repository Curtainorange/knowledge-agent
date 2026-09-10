"""知识条目仓储：当前用户的录入与检索读取，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import func, select
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
        """当前用户所有未删除条目（用于检索）。

        检索需要全量候选，此处刻意不加分页；对外列表展示请用 page_active。
        """
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

    # ---- 对外管理接口（列表 / 更新 / 软删）--------------------------------

    def count_active(self, user_id: str) -> int:
        """当前用户未删除条目总数（用于分页）。"""
        self._guard(user_id)
        stmt = (
            select(func.count())
            .select_from(KnowledgeItem)
            .where(KnowledgeItem.user_id == user_id, KnowledgeItem.is_deleted.is_(False))
        )
        return int(self._session.scalar(stmt) or 0)

    def page_active(self, user_id: str, limit: int = 20, offset: int = 0) -> list[KnowledgeItem]:
        """分页读取未删除条目（列表展示用，避免一次性拉全量）。

        以 id 作二级排序：created_at 只精确到秒，同秒录入的条目分页顺序否则不确定。
        """
        self._guard(user_id)
        stmt = (
            select(KnowledgeItem)
            .where(KnowledgeItem.user_id == user_id, KnowledgeItem.is_deleted.is_(False))
            .order_by(KnowledgeItem.created_at.desc(), KnowledgeItem.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.scalars(stmt))

    def soft_delete(self, item_id: str) -> KnowledgeItem | None:
        """软删（需求安全-5：全表软删 + 保留期物理清除）。"""
        item = self.get(item_id)  # 内含 user_id 越权防护
        if item is None:
            return None
        item.is_deleted = True
        self._session.flush()
        return item

    def apply_update(
        self,
        item: KnowledgeItem,
        *,
        title: str | None = None,
        content: str | None = None,
        tags: list | None = None,
        read_progress: float | None = None,
    ) -> bool:
        """按传入字段局部更新；返回「可检索文本是否变化」以决定是否重算 embedding。"""
        text_changed = False
        if title is not None and title != item.title:
            item.title = title
            text_changed = True
        if content is not None and content != item.raw_content:
            item.raw_content = content
            text_changed = True
        if tags is not None:
            item.tags = list(tags)
        if read_progress is not None:
            item.read_progress = max(0.0, min(1.0, float(read_progress)))
        self._session.flush()
        return text_changed