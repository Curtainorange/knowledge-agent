"""0009 — cost_logs 增 cached_tokens（缓存命中输入 token）。

DeepSeek 的自动上下文缓存命中价约为未命中的 1/50，不单独记账就等于把成本
估高一个量级——L1/L2 的 system 提示词前缀稳定，命中是常态。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_logs",
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("cost_logs", "cached_tokens")