"""书籍对比结果（C 模块）。

## 为什么要落表

一次对比要读两本书的分章要点、做主题对齐、再调一次模型。同一个用户对同一对书
反复问是常见的（"上次那个对比结论在哪"），每次都重算等于重复烧钱。
缓存键是**规范化后的书对**（`pair_key = min_id + '|' + max_id`），
先 A 后 B 还是先 B 后 A 命中同一份结果。

## 为什么存 `aligned`

对齐是"这两章讲的是同一件事"的判断，由 2-gram 相似度算出。
存下来才能回答"凭什么说第三章和第五章是同一个主题"——
不然用户只能看到结论，看不到比较的**边界**（哪些章节没参与比较）。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class BookComparison(Base, TimestampMixin):
    __tablename__ = "book_comparisons"
    __table_args__ = (
        UniqueConstraint("user_id", "pair_key", name="uq_book_comparison_pair"),
        Index("ix_book_comparisons_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    book_a_id: Mapped[str] = mapped_column(String(36))
    book_b_id: Mapped[str] = mapped_column(String(36))
    # 规范化书对：先 A 后 B 与先 B 后 A 命中同一行
    pair_key: Mapped[str] = mapped_column(String(80))

    overview: Mapped[str] = mapped_column(Text, default="")
    # [{topic, relation, a_says, b_says, difference, a_index, b_index}]
    rows: Mapped[list | None] = mapped_column(JSON, default=list)
    # 对齐明细：哪些章节配上了、哪些只有一本有（比较的边界）
    aligned: Mapped[list | None] = mapped_column(JSON, default=list)
    unmatched_a: Mapped[list | None] = mapped_column(JSON, default=list)
    unmatched_b: Mapped[list | None] = mapped_column(JSON, default=list)
