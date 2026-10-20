"""迁移 0018 —— 理解度探查记录（E 模块）。

`comprehension_probes` 存两轮探查的证据：第 1 轮（接触史 + 判断题）、
第 2 轮（复述 + 代码算的覆盖率）。判定结论（gap / mastered / bias / guide /
unknown）也落在这一行，避免"每次读都重新推一遍"。

为什么单独建表而不是塞进 cognitive_diagnoses：探查可以在诊断之前发生、
也可以最后不出诊断（自评"第一次接触"就不该诊断）。塞进诊断表，
这些"没有诊断的探查"就无处可存，等于白问。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "comprehension_probes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("topic", sa.String(128), nullable=False, server_default=""),
        sa.Column("item_id", sa.String(36), nullable=True),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("self_level", sa.String(16), nullable=False, server_default=""),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("options", sa.JSON(), nullable=True),
        sa.Column("judge_key", sa.String(8), nullable=False, server_default=""),
        sa.Column("judge_correct", sa.Boolean(), nullable=True),
        sa.Column("recall_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("coverage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("key_points", sa.JSON(), nullable=True),
        sa.Column("covered", sa.JSON(), nullable=True),
        sa.Column("missed", sa.JSON(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_probes_user_created", "comprehension_probes", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_probes_user_created", table_name="comprehension_probes")
    op.drop_table("comprehension_probes")
