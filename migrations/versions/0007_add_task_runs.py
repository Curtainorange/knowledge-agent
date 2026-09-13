"""0007 — 新增 task_runs 表（异步任务框架：幂等 + 重试 + 死信）。

`idempotency_key` 上的**唯一索引**是幂等得以成立的机制保障：
同一业务动作重复入队时由数据库直接挡回，不依赖应用层的「先查再插」（有竞态）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_name", sa.String(64), nullable=False, index=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_runs_idempotency_key", "task_runs", ["idempotency_key"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_task_runs_idempotency_key", table_name="task_runs")
    op.drop_table("task_runs")