"""计划任务实体（需求 §6.1 PlanTask）：任务拆解。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class PlanTask(Base, TimestampMixin):
    __tablename__ = "plan_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    plan_id: Mapped[str] = mapped_column(String(36), index=True)
    week_index: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending/doing/done
    related_item_ids: Mapped[list | None] = mapped_column(JSON, default=list)