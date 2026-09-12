"""0004 — knowledge_items 增 note 与 source_locator。

支持「阅读时随手写想法」与「回溯原文位置」：
- note：用户自己的批注/思考
- source_locator：来源定位（如书籍内字符区间），用于阅读时高亮已录入区间
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_items", sa.Column("note", sa.Text(), nullable=False, server_default=""))
    op.add_column("knowledge_items", sa.Column("source_locator", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("knowledge_items", "source_locator")
    op.drop_column("knowledge_items", "note")
