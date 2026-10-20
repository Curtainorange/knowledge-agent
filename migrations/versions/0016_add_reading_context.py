"""迁移 0016 —— 会话阅读上下文槽位（B 模块）。

`conversations.reading_context`：进对话后的「需求确认」把阅读目标、进度、
理解程度、想解决的问题、希望的帮助类型落在这个 JSON 列里。

为什么是 JSON 不是多列：这些字段只在确认期使用，不参与查询与索引，
且取值域会随交互迭代——拆成列每改一次枚举就是一次迁移。
可空，旧会话读出来是 NULL，调用方按空上下文处理。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("reading_context", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "reading_context")
