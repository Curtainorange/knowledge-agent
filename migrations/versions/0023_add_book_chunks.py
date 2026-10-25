"""迁移 0023 —— 书籍原文分块（book_chunks），供「就书讨论」检索原句。

通读只留一份几千字的摘要，讨论时模型看不到原句：用户问「第 3 章那个实验的数据是谁做的」，
只能顺着要点编。分块把原文切成 ~1200 字的片段落库，讨论前先检索再喂模型。

`char_start / char_end` 是块在 `books.full_text` 里的区间，也是引用与跳回原文的坐标；
`chapter_index / chapter_title` 冗余一份，检索时不必回表读章节结构。

不加唯一约束：同一本书的块由 `chunking.rebuild()` 先删后建，
块编号（seq）由切块规则决定，规则一改就要整体重算，约束反而挡路。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("book_id", sa.String(36), nullable=False, index=True),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chapter_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chapter_title", sa.String(256), nullable=False, server_default=""),
        sa.Column("char_start", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_end", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_book_chunks_book_user", "book_chunks", ["book_id", "user_id"])


def downgrade() -> None:
    op.drop_index("ix_book_chunks_book_user", table_name="book_chunks")
    op.drop_table("book_chunks")
