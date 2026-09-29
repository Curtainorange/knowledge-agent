"""0015 — 判定质量列（判断层增强）：l2_judgment_logs 加 4 列，conflicts 加撤回列。

- calibrated_confidence：校准后置信度（confidence 保留模型裸自报供前后对比）
- sim：送判时的余弦相似度快照（校准信号 + 评测分析；不落就只能重算）
- review_of_id：非空 = 本行是对该 id 原判的复核结论（日志 append-only，复核写新行）
- review_state：原判行的复核状态 none/pending/upheld/overturned（复核行固定 none）
- conflicts.retracted_at：非空 = 重判/复核推翻后撤回，读路径一律过滤
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("l2_judgment_logs", sa.Column("calibrated_confidence", sa.Float(), nullable=False, server_default="0"))
    op.add_column("l2_judgment_logs", sa.Column("sim", sa.Float(), nullable=True))
    op.add_column("l2_judgment_logs", sa.Column("review_of_id", sa.String(36), nullable=False, server_default=""))
    op.add_column("l2_judgment_logs", sa.Column("review_state", sa.String(16), nullable=False, server_default="none"))
    op.add_column("conflicts", sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conflicts", "retracted_at")
    op.drop_column("l2_judgment_logs", "review_state")
    op.drop_column("l2_judgment_logs", "review_of_id")
    op.drop_column("l2_judgment_logs", "sim")
    op.drop_column("l2_judgment_logs", "calibrated_confidence")
