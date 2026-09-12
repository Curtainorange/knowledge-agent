"""0002 — users 增登录名与密码哈希（鉴权落地，见系统设计 9.3.1）。

- `username` 唯一且必填，作为登录入口
- `password_hash` 存 PBKDF2 串（含算法/迭代数/盐），不存明文

注意：这两列均为非空，若 users 表已有数据需先回填再收紧约束；
当前 P0 的 users 表为空，直接加列即可。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("username", sa.String(64), nullable=False))
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=False))
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "password_hash")
    op.drop_column("users", "username")
