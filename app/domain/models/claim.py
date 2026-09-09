"""主张实体（ADR-09）：单条知识拆解出的原子观点。

持久化主张使冲突检测从 O(n²) 全集比对降为近邻检索，是 L2 冲突检测的基础。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class Claim(Base, TimestampMixin):
    __tablename__ = "claims"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    knowledge_item_id: Mapped[str] = mapped_column(String(36), index=True)
    text: Mapped[str] = mapped_column(Text)  # 主张原文
    confidence: Mapped[float] = mapped_column(Float, default=1.0)  # 主张提取置信度
    # embedding 占位：P1 引入向量库（pgvector / Milvus）时填充，P0 先留空
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)