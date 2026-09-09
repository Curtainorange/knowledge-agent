"""模型调用成本日志（需求维护-1 成本埋点 + 5.2 成本查询接口）。

每次模型调用写一行，可按 user/task/date 归因，支持审计与计费。
仅记录 token 与估算费用等元数据，不含正文（最小化日志）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base


class CostLog(Base):
    __tablename__ = "cost_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    request_id: Mapped[str] = mapped_column(String(64), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0)  # 估算费用（人民币）
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)