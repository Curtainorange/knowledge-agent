"""书籍原文分块（book chunks）—— 让智能体讨论时能查到原句，而不是只凭通读摘要。

## 为什么需要这张表

通读（`book_agent_readings`）是「一次性把全书压缩成要点」：一本书读完只剩几千字笔记，
用户再问「第 3 章那个实验的数据是多少」时，模型手里根本没有那句话，只能顺着要点编。

分块表把原文按 ~1200 字切成可检索的片段，讨论时先检索相关片段再喂模型。
**这是「智能体读过这本书」与「智能体能回答这本书的细节」的分水岭。**

## 为什么不复用 full_text 每次现切

现切能省一张表，但块边界会随切块规则变化而漂移：今天引用「第 12 段」，明天改个阈值
就变成「第 11 段」，引用不可复现。落库保证同一本书的块编号稳定，也便于以后接向量。

## 与用户阅读完全隔离

这张表是只读索引，不参与 `books.current_char` / 阅读进度，也不往 `knowledge_items` 写。
用户划的摘录仍然只走 `BookService.add_note`。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class BookChunk(Base, TimestampMixin):
    __tablename__ = "book_chunks"
    __table_args__ = (
        # 检索永远按「这本书 + 这个用户」取全部块，联合索引正好覆盖
        Index("ix_book_chunks_book_user", "book_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_id: Mapped[str] = mapped_column(String(36), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)  # 全书顺序，从 0 起
    chapter_index: Mapped[int] = mapped_column(Integer, default=0)
    chapter_title: Mapped[str] = mapped_column(String(256), default="")
    # 块在 full_text 里的区间：引用、跳回原文、换算页码都靠它
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, default="")
