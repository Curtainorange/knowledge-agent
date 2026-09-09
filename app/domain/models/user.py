"""用户实体（需求 §6.1 User）：账号、偏好、知识源加密凭证。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    nickname: Mapped[str] = mapped_column(String(64), default="学习者")
    push_frequency: Mapped[str] = mapped_column(String(16), default="weekly")  # weekly/daily/quiet
    # 知识源凭证 AES-256 加密落库；仅使用时解密，禁止写日志（需求安全-2）
    knowledge_credentials: Mapped[str | None] = mapped_column(String, nullable=True)