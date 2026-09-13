"""用户实体（需求 §6.1 User）：账号、偏好、知识源加密凭证。"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    # 登录名：唯一且必填（鉴权入口，见系统设计 9.3.1）
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # PBKDF2 哈希串（含算法/迭代数/盐），**禁止**记录明文或写入日志
    password_hash: Mapped[str] = mapped_column(String(255))
    nickname: Mapped[str] = mapped_column(String(64), default="学习者")
    push_frequency: Mapped[str] = mapped_column(String(16), default="weekly")  # weekly/daily/quiet
    # 知识源凭证 AES-256 加密落库；仅使用时解密，禁止写日志（需求安全-2）
    knowledge_credentials: Mapped[str | None] = mapped_column(String, nullable=True)
    # 令牌版本：改密 / 注销时 +1，已签发的 access 与 refresh 立即失效
    # （JWT 无状态，靠版本号实现吊销，无需额外存储）
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    # 注销采用软删：置时间戳即视为注销，数据保留（审计与保留期），登录与鉴权一律拒绝
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None