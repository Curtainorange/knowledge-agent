"""迁移 0022 —— PDF 页码映射（books.page_marks）。

PDF 被抽成纯文本后，原书的页边界就没了：读者看到的是「第 3 章」，
手里拿的是纸书「第 87 页」，两者对不上，引用也就没法核。

一列 JSON 存「每页在 full_text 里的起点」，形如
`[{"page": 1, "char_start": 0, "label": "3"}]`，`label` 是页面上原印的页码。
**不在正文里插页码标记**：所有 char 偏移都被高亮、摘录、通读分块引用着，
插标记会把已有的每一条记录顶歪。

可空：旧书读出来是 NULL，等价于「这本书没有页码信息」，epub / txt 永远为空。
已有 PDF 想补页码要重新解析一遍（原文件还在 file_path 里），不是本迁移的事。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("books", sa.Column("page_marks", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("books", "page_marks")
