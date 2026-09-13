"""0010 — cost_logs 增 prompt_version（提示词版本追溯）。

E 组 / 风险 R6：改提示词必须同步 bump 版本（见 app/llm/prompts.py），
版本号落库后每次模型调用都能追溯到具体用了哪版提示词。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_logs",
        sa.Column("prompt_version", sa.String(length=16), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("cost_logs", "prompt_version")
