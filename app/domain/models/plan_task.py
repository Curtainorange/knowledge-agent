"""计划任务实体（需求 §6.1 PlanTask）：任务拆解。

F 模块补了三列，让「为什么这样排」可解释：

- `minutes`：预计时长。没有它，"每周排得下吗"根本无法判断。
- `task_type`：`new`（新学）/ `review`（复习）。复习点是规则插入的，不是模型想起来的。
- `basis`：这条任务由哪条规则产生、参数是多少、人话理由是什么（`plan_rules._basis`）。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class PlanTask(Base, TimestampMixin):
    __tablename__ = "plan_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    plan_id: Mapped[str] = mapped_column(String(36), index=True)
    week_index: Mapped[int] = mapped_column(Integer, default=1)
    subject: Mapped[str] = mapped_column(String(256))
    # 这一周要解决什么（原 P0 只有 subject，焦点信息被丢掉了）
    focus: Mapped[str] = mapped_column(Text, default="")
    # 预计时长（分钟）：周容量规则的输入，也是「今天做哪个」的依据
    minutes: Mapped[int] = mapped_column(Integer, default=0)
    # new / review
    task_type: Mapped[str] = mapped_column(String(16), default="new")
    # {"rule": "weekly_capacity", "why": "...", "params": {...}}
    basis: Mapped[dict | None] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")  # pending/doing/done
    related_item_ids: Mapped[list | None] = mapped_column(JSON, default=list)