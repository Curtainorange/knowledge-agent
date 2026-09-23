"""0012 — knowledge_items 增 embedding_model / embedding_dim。

记录生成向量的嵌入模型标识与维度（借鉴 agentic-local-brain 的供应商耦合教训）：
- 换嵌入模型后旧向量不可比，据此字段做**增量重建**而不是全库盲刷；
- NULL = 旧版本写入，模型未知，重建时一并处理。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("embedding_dim", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("knowledge_items", "embedding_dim")
    op.drop_column("knowledge_items", "embedding_model")
