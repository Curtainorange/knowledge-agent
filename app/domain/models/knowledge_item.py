"""知识条目实体（需求 §6.1 KnowledgeItem）：知识库基本单位。"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Float, String, Text
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
    # embedding 向量（pickle 序列化 bytes）；MVP 存入关系库，检索时加载做余弦；P2 迁移 pgvector
    embedding: Mapped[bytes | None] = mapped_column(nullable=True)
    # 向量化状态：pending=待算 / embedded=可检索 / embed_failed=失败（降级关键词召回）
    embed_status: Mapped[str] = mapped_column(default="pending")
    # 用户自己的批注 / 想法（阅读时随手写下，与摘录原文一起沉淀为一条知识）
    note: Mapped[str] = mapped_column(Text, default="")
    # 来源定位：如书籍内的字符区间 {chapter_index, char_start, char_end}，
    # 用于回溯原文、以及阅读时高亮「已录入」的区间
    source_locator: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # L2 冲突检测：主张提取的扫描标记（NULL = 尚未提取）。
    # 用于**增量扫描**——只遍历新增条目，避免每次扫描都重跑全量主张提取（成本与耗时）
    claims_scanned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # 软删（需求安全-5 / §6.3：全表软删 + 保留期物理清除）
    is_deleted: Mapped[bool] = mapped_column(default=False)

    @property
    def snippet(self) -> str:
        """列表 / 定位场景的定长摘要（不落库、不参与向量化）。"""
        text = (self.raw_content or "").replace("\n", " ").strip()
        return text[:120] + ("…" if len(text) > 120 else "")

    @property
    def is_readable(self) -> bool:
        """是否已读完（用于 L1 定位后的复用提醒，避免恒真的假信号）。"""
        return self.read_progress >= 1.0