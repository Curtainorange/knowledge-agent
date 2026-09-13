"""异步任务运行记录（ADR-13 幂等 + 可靠性工程）。

单个进程内的最小可用任务队列：入队即落一行，由 worker 轮询执行。

- `idempotency_key` 唯一索引是**幂等的载体**：同一业务动作（如同一用户同一小时的
  L2 扫描、同一用户同一周的周扫）重复入队时直接被唯一约束挡回，无需额外的锁或状态机。
  这也让「周扫」这类周期性触发退化成一句「确保某 key 存在」的查询。
- 状态机：pending →（领取）running → succeeded；失败则回到 pending 等下轮重试，
  重试次数达 max_attempts 转 dead（死信，保留现场供排查，不再自动重试）。
- `last_error` 只记异常摘要，不落正文（最小化日志）。
"""
from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin

# pending 待执行 / running 执行中 / succeeded 成功 / dead 死信（重试耗尽）
TASK_STATES = ("pending", "running", "succeeded", "dead")


class TaskRun(Base, TimestampMixin):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    task_name: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    # 唯一键：同一 key 只允许一行。unique+index 必须同时声明——
    # 迁移路径靠 0007 的显式索引，dev/test 的 create_all 路径靠这里，
    # 两边不一致会让幂等保障在某些环境下静默失效。
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试便利
        return f"<TaskRun {self.task_name} {self.status} attempts={self.attempts}>"