"""0011 — 新增 push_jobs / push_logs / cognitive_diagnoses 三表（ADR-14 + L5）。

- push_jobs：推送任务生命周期（pending/suppressed/sent/delivered/failed）
- push_logs：推送审计流水；`content_hash` 唯一索引是防骚扰去重的机制保障
- cognitive_diagnoses：L5 归因诊断报告（pattern/root_cause/confidence/...）
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("push_type", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="app"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("title", sa.String(255), nullable=False, server_default=""),
        sa.Column("body", sa.String(2000), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False, index=True),
        sa.Column("sent_at", sa.String(64), nullable=True),
        sa.Column("last_error", sa.String(1000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "push_logs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("push_type", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="app"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_feedback", sa.String(16), nullable=False, server_default="none"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_push_logs_content_hash", "push_logs", ["content_hash"], unique=True)

    op.create_table(
        "cognitive_diagnoses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("pattern", sa.String(64), nullable=False, server_default=""),
        sa.Column("root_cause", sa.String(2000), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("suggested_action", sa.String(2000), nullable=False, server_default=""),
        sa.Column("reasoning_chain", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("cognitive_diagnoses")
    op.drop_index("ix_push_logs_content_hash", table_name="push_logs")
    op.drop_table("push_logs")
    op.drop_table("push_jobs")
