"""L2 判定日志：每一对送判的主张的判定结果（含非矛盾）。

为什么落库：判定是非矛盾时**不入冲突库、不留任何痕迹**——排查「为什么没抓出冲突」
只能重判重烧钱（2026-09-29 排查影子主张时实测连跑 4 轮扫描）。有了这份日志，
每轮判定的事实（判了哪些对、判了什么、置信度多少）都可追溯。

自包含设计：标题与主张文本直接落库——影子主张会被重扫替换、笔记可能软删，
靠 claim_id 回查会静默丢文本。日志是排查事实，不是外键关系数据。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Float, Index, String, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class L2JudgmentLog(Base, TimestampMixin):
    __tablename__ = "l2_judgment_logs"
    __table_args__ = (Index("ix_l2_judgments_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    # 无序主张对键（make_pair_key），与 conflicts.pair_key 同一口径
    pair_key: Mapped[str] = mapped_column(String(80))
    claim_a_id: Mapped[str] = mapped_column(String(36))
    claim_b_id: Mapped[str] = mapped_column(String(36))
    # 来源键：知识条目 id 或 book:<book_id>（判定 book 标题与主张文本随行落库）
    source_a: Mapped[str] = mapped_column(String(72))
    source_b: Mapped[str] = mapped_column(String(72))
    title_a: Mapped[str] = mapped_column(String(256), default="")
    title_b: Mapped[str] = mapped_column(String(256), default="")
    claim_a_text: Mapped[str] = mapped_column(Text, default="")
    claim_b_text: Mapped[str] = mapped_column(Text, default="")
    relation: Mapped[str] = mapped_column(String(8))          # 矛盾 | 互补 | 断层 | 无关
    conflict_type: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # 校准后置信度（判断层）：confidence 保留模型裸自报，校准值供阈值与评测对比
    calibrated_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # 送判时的余弦相似度快照（校准信号 + 评测分析；topic 通道可能没有）
    sim: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 非空 = 本行是对该 id 原判的复核结论（日志 append-only，复核写新行不覆盖）
    review_of_id: Mapped[str] = mapped_column(String(36), default="")
    # 原判行的复核状态：none（不需复核）/ pending（预算耗尽或复核失败）/
    # upheld（复核维持）/ overturned（复核推翻）。复核行自身固定 none
    review_state: Mapped[str] = mapped_column(String(16), default="none")
    polarity_a: Mapped[int] = mapped_column(SmallInteger, default=0)
    polarity_b: Mapped[int] = mapped_column(SmallInteger, default=0)
    detail: Mapped[str] = mapped_column(Text, default="")
