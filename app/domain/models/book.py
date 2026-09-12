"""书籍实体（阅读器）：用户上传的电子书与阅读进度。

文件本体存本地 `data/books/`，本表只存元数据与解析后的章节结构；
`full_text` 落一份去标签后的全文，供阅读切片与划词检索使用（P0 体量下可接受，
量级上来后可改存对象存储，仅保留字符偏移索引）。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class Book(Base, TimestampMixin):
    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    title: Mapped[str] = mapped_column(String(256))
    author: Mapped[str] = mapped_column(String(128), default="")
    format: Mapped[str] = mapped_column(String(8))  # txt | epub
    file_path: Mapped[str] = mapped_column(String(512))  # 本地存储路径（原始文件）
    # 章节结构：[{index, title, char_start, char_end}]，char 为 full_text 内的字符偏移
    chapters: Mapped[list | None] = mapped_column(JSON, default=list)
    full_text: Mapped[str] = mapped_column(Text, default="")  # 去标签后的全文
    total_chars: Mapped[int] = mapped_column(Integer, default=0)
    current_char: Mapped[int] = mapped_column(Integer, default=0)  # 阅读位置（字符偏移）
    read_progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..1
    is_deleted: Mapped[bool] = mapped_column(default=False)
