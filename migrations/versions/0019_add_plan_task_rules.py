"""迁移 0019 —— 计划任务的时长 / 类型 / 依据（F 模块）。

原 `plan_tasks` 只有 `week_index + subject + status`，于是：

- **"每周排得下吗"无法判断**——没有时长，周容量规则没有输入；
- **复习点无处安放**——没法区分"新学"和"回看"，第 4 周该复习什么只能靠模型自觉；
- **"为什么排在这里"说不出来**——用户看得见任务，看不见理由。

三列之后，这三件事都变成可判定、可展示的。`basis` 存规则名 + 参数 + 人话理由，
前端用它做那个"为什么排这里"的抽屉。

全部可空 / 有默认值：旧计划读出来时长是 0、类型是 new，前端按"未计算"显示。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plan_tasks", sa.Column("focus", sa.Text(), nullable=True))
    op.add_column(
        "plan_tasks", sa.Column("minutes", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "plan_tasks",
        sa.Column("task_type", sa.String(16), nullable=False, server_default="new"),
    )
    op.add_column("plan_tasks", sa.Column("basis", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("plan_tasks", "basis")
    op.drop_column("plan_tasks", "task_type")
    op.drop_column("plan_tasks", "minutes")
    op.drop_column("plan_tasks", "focus")
