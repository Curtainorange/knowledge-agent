"""会话实体（需求 §6.1 Conversation）：对话状态机 + 上下文。

P0 只承载最小聊天流的上下文；L1 多轮澄清 / 状态迁移后续在状态上扩展。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    state: Mapped[str] = mapped_column(String(32), default="idle")  # 见 6.3 状态机
    title: Mapped[str] = mapped_column(String(128), default="")
    # 会话消息历史：[{"role": ..., "content": ...}, ...]，驱动多轮上下文
    messages: Mapped[list | None] = mapped_column(JSON, default=list)
    # 阅读上下文槽位（B 模块）：进对话后的「需求确认」把 5 个确认项落在这里。
    # 存 JSON 而不是拆 8 个列：这些字段只在确认期使用，不参与查询与索引，
    # 且取值域会随交互迭代——拆成列每改一次就要一次迁移。
    reading_context: Mapped[dict | None] = mapped_column(JSON, default=dict)