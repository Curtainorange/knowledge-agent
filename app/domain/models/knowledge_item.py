"""知识条目实体（需求 §6.1 KnowledgeItem）：知识库基本单位。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class KnowledgeItem(Base, TimestampMixin):
    __tablename__ = "knowledge_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(String(32))  # notion / obsidian / feishu / bookmark / manual
    source_item_id: Mapped[str] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    raw_content: Mapped[str] = mapped_column(Text)  # 原文（生产按 ADR-03 入对象存储快照）
    tags: Mapped[list | None] = mapped_column(JSON, default=list)
    read_progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1 阅读进度
    # 软删（需求安全-5 / §6.3：全表软删 + 保留期物理清除）
    is_deleted: Mapped[bool] = mapped_column(default=False)