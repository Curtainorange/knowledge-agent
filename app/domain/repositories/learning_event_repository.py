"""行为事件仓储（ADR-10）：只追加的 LearningEvent 存取。

L4 的行为偏离检测与 L5 的归因诊断完全以这张表为原材料，因此它是"越早埋越值钱"
的基础设施。仓储只负责追加与读取，不做任何聚合（聚合属能力层）。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.learning_event import LearningEvent
from app.domain.repositories.base import BaseRepository


class LearningEventRepository(BaseRepository[LearningEvent]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def append(
        self,
        *,
        user_id: str,
        event_type: str,
        payload: dict | None = None,
        occurred_at=None,
    ) -> LearningEvent:
        """追加一条事件（append-only，不提供更新与删除）。"""
        from datetime import datetime, timezone

        event = LearningEvent(
            user_id=user_id,
            event_type=event_type,
            occurred_at=occurred_at or datetime.now(timezone.utc).replace(tzinfo=None),
            payload=payload or {},
        )
        self._session.add(event)
        self._session.flush()
        return event

    def list_recent(
        self, user_id: str, *, event_type: str | None = None, limit: int = 50
    ) -> list[LearningEvent]:
        self._guard(user_id)
        stmt = select(LearningEvent).where(LearningEvent.user_id == user_id)
        if event_type:
            stmt = stmt.where(LearningEvent.event_type == event_type)
        stmt = stmt.order_by(
            LearningEvent.occurred_at.desc(), LearningEvent.id.asc()
        ).limit(limit)
        return list(self._session.scalars(stmt))

    def list_since(
        self,
        user_id: str,
        *,
        since,
        event_type: str | None = None,
        limit: int = 5000,
    ) -> list[LearningEvent]:
        """取某个时间点之后的全部事件（按时间升序，供按天聚合用）。"""
        self._guard(user_id)
        stmt = select(LearningEvent).where(
            LearningEvent.user_id == user_id, LearningEvent.occurred_at >= since
        )
        if event_type:
            stmt = stmt.where(LearningEvent.event_type == event_type)
        stmt = (
            stmt.order_by(LearningEvent.occurred_at.asc(), LearningEvent.id.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def count_by_type(self, user_id: str) -> dict[str, int]:
        """按事件类型计数——用于埋点完整率监控（架构 §10.2）。"""
        from sqlalchemy import func

        self._guard(user_id)
        stmt = (
            select(LearningEvent.event_type, func.count())
            .where(LearningEvent.user_id == user_id)
            .group_by(LearningEvent.event_type)
        )
        return {str(t): int(c) for t, c in self._session.execute(stmt)}