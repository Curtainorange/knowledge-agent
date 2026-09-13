"""行为事件查询端点：给埋点一个"看得见"的出口。

用途不是给用户看，而是验证埋点是否真的在写、以及各类型事件的分布是否合理
（架构 §10.2 的埋点完整率监控）。L4/L5 落地前后，这是最直接的排查入口。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core import trace
from app.domain.repositories.learning_event_repository import LearningEventRepository

router = APIRouter(prefix="/api/v1/events", tags=["events"])


class EventOut(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime
    payload: dict = Field(default_factory=dict)


class EventListResponse(BaseModel):
    items: list[EventOut]
    total: int
    by_type: dict[str, int]
    request_id: str


@router.get("", response_model=EventListResponse)
def list_events(
    event_type: str | None = Query(default=None, description="按事件类型过滤，如 l1.mine"),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> EventListResponse:
    """当前用户最近的行为事件（只读）。"""
    repo = LearningEventRepository(session, user_id=user_id)
    rows = repo.list_recent(user_id, event_type=event_type, limit=limit)
    return EventListResponse(
        items=[
            EventOut(
                event_id=row.id,
                event_type=row.event_type,
                occurred_at=row.occurred_at,
                payload=row.payload or {},
            )
            for row in rows
        ],
        total=len(rows),
        by_type=repo.count_by_type(user_id),
        request_id=trace.get_request_id() or "",
    )