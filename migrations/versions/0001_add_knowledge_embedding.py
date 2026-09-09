"""0001 — knowledge_items 增 embedding 向量（BLOB）与 embed_status。

L1 MVP：检索依赖 embedding。向量 MVP 存关系库 BLOB、检索时载入做余弦；
embeded_status 标记向量化状态（pending/embedded/embed_failed），供降级判断。

Revision ID: 0001
Revises:
Create Date: 2026-09-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_items", sa.Column("embedding", sa.LargeBinary(), nullable=True))
    op.add_column("knowledge_items", sa.Column("embed_status", sa.String(), nullable=False, server_default="pending"))


def downgrade() -> None:
    op.drop_column("knowledge_items", "embed_status")
    op.drop_column("knowledge_items", "embedding")