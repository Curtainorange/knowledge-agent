"""推送日志实体（系统设计 §4.1 ⑧ PushLog）：推送的追溯与 A/B 调整依据。

与 PushJob 的分工：PushJob 是「待发送任务」的生命周期，PushLog 是「已发生推送」
的审计流水。`content_hash` 唯一约束在**模型与迁移两处都声明**（create_all 与
alembic 两条建表路径必须一致，否则某环境静默失去去重——同 09-12「漏 import」
与「幂等约束只写一处」是同一类陷阱）。
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin

FEEDBACK_STATES = ("none", "ignored", "accepted")  # 未响应 / 忽略 / 采纳


class PushLog(Base, TimestampMixin):
    __tablename__ = "push_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    push_type: Mapped[str] = mapped_column(String(32))  # conflict/question/brief/diagnosis
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)  # 去重指纹
    channel: Mapped[str] = mapped_column(String(16), default="app")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_feedback: Mapped[str] = mapped_column(String(16), default="none")  # none/ignored/accepted
