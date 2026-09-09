"""学习行为事件实体（ADR-10）：只追加（append-only）的行为事件源。

L4 行为偏离 / L5 归因诊断均以 LearningEvent 为原材料。
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class LearningEvent(Base, TimestampMixin):
    __tablename__ = "learning_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)  # read/collect/ask/complete_plan/...
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)  # 事件详情（不含正文/敏感字段）