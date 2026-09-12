"""0006 — 新增 claims 与 conflicts 表（L2 冲突检测基础）。

L2 冲突检测以「主张（Claim）」为最小比对粒度：先抽取 → 同主题预筛 → 候选对生成 →
两两比对判冲突 → 落 `conflicts` 表供推送与用户反馈。

两张表均强制 user_id 索引与跨表索引，与 ADR-09 / 需求 §6 ⑤ 对齐。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("knowledge_item_id", sa.String(36), nullable=False, index=True),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("topic", sa.String(128), nullable=False, server_default=""),
        sa.Column("polarity", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("strength", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_claims_user_topic", "claims", ["user_id", "topic"])

    op.create_table(
        "conflicts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("item_a_id", sa.String(36), nullable=False, index=True),
        sa.Column("item_b_id", sa.String(36), nullable=False, index=True),
        sa.Column("claim_a_id", sa.String(36), nullable=True),
        sa.Column("claim_b_id", sa.String(36), nullable=True),
        sa.Column("pair_key", sa.String(80), nullable=False, index=True),
        sa.Column("conflict_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("suggestion", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("user_state", sa.String(16), nullable=False, server_default="unseen"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conflicts_user_state", "conflicts", ["user_id", "user_state"])


def downgrade() -> None:
    op.drop_index("ix_conflicts_user_state", table_name="conflicts")
    op.drop_table("conflicts")
    op.drop_index("ix_claims_user_topic", table_name="claims")
    op.drop_table("claims")