"""0003 — 新增 books 表（阅读器）。

书籍元数据 + 解析后的章节结构 + 去标签全文 + 阅读进度。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "books",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("author", sa.String(128), nullable=False, server_default=""),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("file_path", sa.String(512), nullable=False),
        sa.Column("chapters", sa.JSON(), nullable=True),
        sa.Column("full_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("total_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_char", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_progress", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("books")
