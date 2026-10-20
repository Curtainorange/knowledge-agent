"""迁移 0020 —— 书籍对比缓存（C 模块）。

`book_comparisons` 存一次对比的完整结果：总评、逐主题对比行、以及**对齐明细**。

缓存键是规范化书对（`pair_key`），同一对书只保留一行（重算覆盖）。
存 `aligned / unmatched_*` 是为了让"哪些章节参与了比较"可回溯——
只给结论、不给比较范围，用户无法判断这个对比靠不靠谱。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "book_comparisons",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False, index=True),
        sa.Column("book_a_id", sa.String(36), nullable=False),
        sa.Column("book_b_id", sa.String(36), nullable=False),
        sa.Column("pair_key", sa.String(80), nullable=False),
        sa.Column("overview", sa.Text(), nullable=False, server_default=""),
        sa.Column("rows", sa.JSON(), nullable=True),
        sa.Column("aligned", sa.JSON(), nullable=True),
        sa.Column("unmatched_a", sa.JSON(), nullable=True),
        sa.Column("unmatched_b", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "pair_key", name="uq_book_comparison_pair"),
    )
    op.create_index("ix_book_comparisons_user", "book_comparisons", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_book_comparisons_user", table_name="book_comparisons")
    op.drop_table("book_comparisons")
