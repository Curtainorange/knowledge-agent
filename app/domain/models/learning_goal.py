"""学习目标实体（需求 §6.1 LearningGoal）：目标管理。"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class LearningGoal(Base, TimestampMixin):
    __tablename__ = "learning_goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    description: Mapped[str] = mapped_column(String(512))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority: Mapped[str] = mapped_column(String(16), default="medium")  # high/medium/low
    achieved: Mapped[bool] = mapped_column(default=False)