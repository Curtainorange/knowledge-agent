"""主张实体（ADR-09）：单条知识拆解出的原子观点。

持久化主张使冲突检测从 O(n²) 全集比对降为近邻检索，是 L2 冲突检测的基础。

字段按《系统设计文档》§⑥ 对齐：
- `statement` 是归一化后的可比对主张陈述，即冲突判定的**最小比对粒度**
- `topic` 与 `user_id` 组成「同主题预筛」索引（冲突漏斗 L2-3）
- `polarity` / `strength` 记录立场极性（-1 否定 / 0 中性 / 1 肯定）与表述强度，
  作为冲突判定的上下文提示，并用于候选对排序
- `embedding` 让候选对生成可以走向量近邻，而不只依赖主题词的字面一致
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Float, Index, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class Claim(Base, TimestampMixin):
    __tablename__ = "claims"
    __table_args__ = (Index("ix_claims_user_topic", "user_id", "topic"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    knowledge_item_id: Mapped[str] = mapped_column(String(36), index=True)
    # 归一化后的可比对主张陈述
    statement: Mapped[str] = mapped_column(Text)
    topic: Mapped[str] = mapped_column(String(128), default="")  # 主题标签（同主题预筛）
    polarity: Mapped[int] = mapped_column(SmallInteger, default=0)  # -1 否定 / 0 中性 / 1 肯定
    strength: Mapped[float] = mapped_column(Float, default=0.5)     # 表述强度 0..1
    confidence: Mapped[float] = mapped_column(Float, default=0.5)   # 抽取置信度 0..1
    # 主张向量（pickle 序列化 bytes）；用于候选对生成的向量近邻检索
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)

    @property
    def stance(self) -> str:
        """极性的人类可读形式（进提示词与前端展示）。"""
        return {-1: "否定", 0: "中性", 1: "肯定"}.get(int(self.polarity or 0), "中性")
