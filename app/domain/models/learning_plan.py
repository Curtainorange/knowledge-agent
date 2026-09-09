"""学习计划实体（需求 §6.1 LearningPlan）：目标关联、结构化计划内容。

P0 只占位建实体；L4 计划生成/重规划在后续实现。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class LearningPlan(Base, TimestampMixin):
    __tablename__ = "learning_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    goal_id: Mapped[str] = mapped_column(String(36), index=True)
    content: Mapped[dict | None] = mapped_column(JSON, default=dict)  # 结构化计划内容
    version: Mapped[int] = mapped_column(default=1)  # 乐观锁版本号（可靠-2）