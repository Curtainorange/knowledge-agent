"""0005 — knowledge_items 增 claims_scanned_at。

支撑 L2 冲突检测的「主张提取扫描标记」：
- claims_scanned_at：NULL = 尚未做主张提取。
  用于增量扫描——只遍历新增条目，避免每次扫描重跑全量提取。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("claims_scanned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_items", "claims_scanned_at")