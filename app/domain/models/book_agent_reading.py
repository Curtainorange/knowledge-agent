"""智能体通读记录（book agent readings）——「智能体读的书」与「用户读的书」隔离。

用户阅读的痕迹有两处：`books.read_progress / current_char`（阅读进度）和
`knowledge_items`（划词摘录进知识库）。智能体通读全书**两边都不碰**——
它的阅读产出（总评 + 分章要点）只落在这张独立的表里。

每个 (user_id, book_id) 只保留**最新一份**通读结果：重读是覆盖式 upsert，
不做历史版本（通读笔记是讨论用的上下文，旧版没有回看价值）。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class BookAgentReading(Base, TimestampMixin):
    __tablename__ = "book_agent_readings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(16), default="done")  # done | failed
    # 通读时的全书长度：书没变（total_chars 相同）则可复用，不必重新花钱重读
    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")  # 全书总评
    # 分章要点：[{index, title, gist, points: [...]}]
    chapters_note: Mapped[list | None] = mapped_column(JSON, default=list)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)  # 实际读过的块数
    failed_chunks: Mapped[int] = mapped_column(Integer, default=0)  # 要点提取失败的块数
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)  # 超长书被截断
    is_deleted: Mapped[bool] = mapped_column(default=False)
