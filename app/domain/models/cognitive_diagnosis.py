"""认知诊断报告实体（系统设计 §4.1 ④ CognitiveDiagnosis）：L5 归因诊断产出。

诊断是 L5 的交付物：不止给行为统计，还链式推理出行为背后的认知病根 + 可执行方案。
`reasoning_chain` 存简要推理链（CoT）以增强可信度；`status` 让用户可采纳 / 拒绝，
采纳后回写计划系统形成闭环。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin

DIAGNOSIS_STATES = ("pending", "accepted", "rejected")


class CognitiveDiagnosis(Base, TimestampMixin):
    __tablename__ = "cognitive_diagnoses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    pattern: Mapped[str] = mapped_column(String(64), default="")  # 行为模式，如「高收藏低完成」
    root_cause: Mapped[str] = mapped_column(String(2000), default="")
    confidence: Mapped[float] = mapped_column(default=0.5)
    suggested_action: Mapped[str] = mapped_column(String(2000), default="")
    # 简要推理链（CoT），增强可信度（§5.5 技术要点）
    reasoning_chain: Mapped[list | None] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/accepted/rejected
