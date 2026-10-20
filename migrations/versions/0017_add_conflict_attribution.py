"""迁移 0017 —— 冲突归因（D 模块）。

`conflicts` 加 4 列：

- `attribution`：为什么对立的结构化原因（背景 / 前提 / 边界 / 冲突类型）
- `scene_guidance`：什么场景听谁、为什么
- `probe`：用户自己能验证的一条判据
- `attributed_at`：非空 = 已归因，作为幂等锚点（重复点开不再重复调模型）

原表只有 `detail` / `suggestion` 两段自由文本，模型只能产出「两本书说法对立，
建议你对谈一下」。这四列之后，「为什么对立」才成为可校验、可展示的结构。

全部可空：旧冲突读出来是 NULL，前端按「尚未分析」处理。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conflicts", sa.Column("attribution", sa.JSON(), nullable=True))
    op.add_column("conflicts", sa.Column("scene_guidance", sa.JSON(), nullable=True))
    op.add_column("conflicts", sa.Column("probe", sa.JSON(), nullable=True))
    op.add_column("conflicts", sa.Column("attributed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("conflicts", "attributed_at")
    op.drop_column("conflicts", "probe")
    op.drop_column("conflicts", "scene_guidance")
    op.drop_column("conflicts", "attribution")
