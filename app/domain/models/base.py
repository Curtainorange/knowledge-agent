"""SQLAlchemy 声明基类与公共列。"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# 主键：UUID 字符串（36 位），显式以便对外接口不必暴露对应 DB 列细节
# （对用户/计划/会话等业务实体使用；CostLog 用自增 int）
PkStr = Annotated[str, mapped_column(primary_key=True)]


class TimestampMixin:
    """通用创建/更新时间戳。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )