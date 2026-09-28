"""0013 — 新增 book_agent_readings 表（智能体通读记录）。

「智能体读的书」与「用户读的书」隔离：用户的阅读进度在 books 表、
划词摘录在 knowledge_items；智能体通读全书的产出（总评 + 分章要点）
只落这张独立表，两边互不污染。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_agent_readings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("book_id", sa.String(36), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="done"),
        sa.Column("total_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("chapters_note", sa.JSON(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("book_agent_readings")
