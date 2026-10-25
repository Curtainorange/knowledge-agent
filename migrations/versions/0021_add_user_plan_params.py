"""迁移 0021 —— 用户可自定义排期参数（F 模块的「可改」）。

10-20 的方案要求计划页「显示当前参数值**并可改**」，实际只做了显示：
`PlanParams` 硬编码在 `DEFAULT_PARAMS`，用户没有任何入口调整每周可用时间。
后果是结构性的——一个每周只有两小时的人，拿着「每周 5 小时」的容量，
计划从第一天就排不完；排不完的计划等于没有计划。

一列 JSON 而不是四列：这组值一起读一起写，拆成列会让「加一个参数」变成一次迁移。
读取一律过 `plan_rules.from_prefs()` 收敛，所以列里存什么形状都不至于让计划页崩。

可空：旧用户读出来是 NULL，等价于「用默认值」，不需要回填。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("plan_params", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "plan_params")
