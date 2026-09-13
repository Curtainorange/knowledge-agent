"""0008 — users 增令牌版本与注销时间戳。

- `token_version`：JWT 是无状态的，签出去就收不回来。改密 / 注销时把版本 +1，
  校验端比对令牌里的 `ver` 与用户当前版本，即可让全部旧令牌立即失效——
  无需引入黑名单存储，也无需等过期。
- `deleted_at`：注销采用软删（保留数据供审计与保留期），置位后登录与鉴权一律拒绝。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deleted_at")
    op.drop_column("users", "token_version")