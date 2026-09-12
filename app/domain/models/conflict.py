"""观点冲突实体（需求 §6 ⑤ / UC-L2-01）：L2 冲突检测的产物。

- `claim_a_id` / `claim_b_id` 记录冲突判定的**最小比对粒度**，便于复盘与误报分析；
  `item_a_id` / `item_b_id` 保留条目级溯源（前端展示与跳转用）
- `pair_key` 是两个主张 id 排序拼接的去重键：同一对主张只入库一次，
  重复扫描不会产生重复冲突
- `user_state` 承载用户反馈（UC-L2-03）：unseen / ignored / accepted。
  「忽略同类型 ≥N 次」用于收敛该类冲突推荐（误报抑制）
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class Conflict(Base, TimestampMixin):
    __tablename__ = "conflicts"
    # 待推送冲突查询（系统设计 §实体索引：conflict(user_id, user_state)）
    __table_args__ = (Index("ix_conflicts_user_state", "user_id", "user_state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)

    item_a_id: Mapped[str] = mapped_column(String(36), index=True)
    item_b_id: Mapped[str] = mapped_column(String(36), index=True)
    claim_a_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    claim_b_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 无序对键：sorted(claim_a_id, claim_b_id) 拼接，用于避免同一对主张重复入库
    pair_key: Mapped[str] = mapped_column(String(80), index=True)

    conflict_type: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[str] = mapped_column(Text, default="")       # 冲突细节描述
    suggestion: Mapped[str] = mapped_column(Text, default="")   # 建议动作
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # unseen / ignored / accepted
    user_state: Mapped[str] = mapped_column(String(16), default="unseen")
