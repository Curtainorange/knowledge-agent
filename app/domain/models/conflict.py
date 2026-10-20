"""观点冲突实体（需求 §6 ⑤ / UC-L2-01）：L2 冲突检测的产物。

- `claim_a_id` / `claim_b_id` 记录冲突判定的**最小比对粒度**，便于复盘与误报分析；
  `item_a_id` / `item_b_id` 保留条目级溯源（前端展示与跳转用）
- `pair_key` 是两个主张 id 排序拼接的去重键：同一对主张只入库一次，
  重复扫描不会产生重复冲突
- `user_state` 承载用户反馈（UC-L2-03）：unseen / ignored / accepted。
  「忽略同类型 ≥N 次」用于收敛该类冲突推荐（误报抑制）
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, Index, String, Text
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
    # 非空 = 重判/复核推翻后撤回（判断层翻案通道）。所有读路径过滤已撤回
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- 归因（D 模块）--------------------------------------------------
    # 原实现只有 detail / suggestion 两段自由文本，模型只能写「两本书说法对立，
    # 建议你对谈一下」——解释了现象，没解释原因。以下四列把「为什么对立」结构化：
    #
    # `attribution` = {cause_type, background_a, background_b, premise_a, premise_b,
    #                  boundary_a, boundary_b}
    #   background 是**必填**：作者立场、时代、学科前提、写作目的。缺了它，
    #   「为什么对立」只能退化成一句「他们看法不同」。
    # `scene_guidance` = [{scene, follow, reason}]：什么场景听谁、为什么
    # `probe` = {question, how_to_check}：用户自己能验证的一条判据
    attribution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    scene_guidance: Mapped[list | None] = mapped_column(JSON, default=list)
    probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 非空 = 已归因。幂等锚点：重复点开不再重复花模型的钱
    attributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
