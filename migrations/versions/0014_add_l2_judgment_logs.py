"""0014 — 新增 l2_judgment_logs 表（L2 判定明细日志）。

非矛盾的判定不入冲突库、不留痕迹，排查「为什么没抓出冲突」只能重判重烧钱。
本表落每一对送判主张的判定结果（relation / confidence / 双方标题与文本），
自包含（不回查 claim），供排查与成本审计。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "l2_judgment_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("pair_key", sa.String(80), nullable=False),
        sa.Column("claim_a_id", sa.String(36), nullable=False),
        sa.Column("claim_b_id", sa.String(36), nullable=False),
        sa.Column("source_a", sa.String(72), nullable=False),
        sa.Column("source_b", sa.String(72), nullable=False),
        sa.Column("title_a", sa.String(256), nullable=False, server_default=""),
        sa.Column("title_b", sa.String(256), nullable=False, server_default=""),
        sa.Column("claim_a_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("claim_b_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("relation", sa.String(8), nullable=False),
        sa.Column("conflict_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("polarity_a", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("polarity_b", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_l2_judgments_user_created", "l2_judgment_logs", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_l2_judgments_user_created", table_name="l2_judgment_logs")
    op.drop_table("l2_judgment_logs")
