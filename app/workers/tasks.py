"""最小可用异步任务框架（ADR-13 幂等 / 可靠性工程）。

不引入 Celery / Redis：单进程内「落库排队 + worker 轮询」即可满足 P0 的周扫与
事件触发需求。三条机制各有明确落点：

- **幂等**：`TaskRun.idempotency_key` 唯一约束。重复入队由数据库挡回
  （IntegrityError → 视为已存在），不依赖应用层「先查再插」那种有竞态的做法。
  副作用是「周扫」变得极其简单：只需确保 `l2:weekly:{user}:{ISO周}` 这个 key 存在，
  重复调用自然只执行一次。
- **重试**：失败不改状态为 dead，而是留在 pending、attempts+1，下一轮轮询再试；
  退避用 `updated_at`（失败时被 onupdate 刷新）作时间锚点，`2^(n-1)` 起步、
  上限 60 秒，省掉额外的 last_attempt_at 列。
- **死信**：attempts ≥ max_attempts 转 `dead` 保留现场（last_error），不再自动重试。

处理器注册表 `HANDLERS` 由各能力模块自行注册，框架不感知业务。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.domain.models.task_run import TaskRun

logger = logging.getLogger(__name__)

Handler = Callable[[dict, Session], None]

# task_name → 处理函数（由各个能力模块注册）
HANDLERS: dict[str, Handler] = {}


def register(name: str) -> Callable[[Handler], Handler]:
    """注册任务处理器。"""

    def decorator(fn: Handler) -> Handler:
        if name in HANDLERS:
            raise ValueError(f"任务处理器重复注册：{name}")
        HANDLERS[name] = fn
        return fn

    return decorator


@dataclass
class EnqueueResult:
    created: bool
    task_id: str = ""
    task_name: str = ""


@dataclass
class RunSummary:
    claimed: int = 0
    succeeded: int = 0
    retried: int = 0
    dead: int = 0
    skipped: int = 0
    ran: list[str] = field(default_factory=list)


def enqueue(
    session: Session,
    *,
    task_name: str,
    user_id: str,
    payload: dict | None = None,
    idempotency_key: str,
    max_attempts: int | None = None,
) -> EnqueueResult:
    """幂等入队：同 key 已存在则返回 created=False（不新建、不报错）。"""
    from app.core.config import settings

    row = TaskRun(
        task_name=task_name,
        user_id=user_id,
        idempotency_key=idempotency_key,
        status="pending",
        attempts=0,
        max_attempts=max_attempts or settings.task_max_attempts,
        payload=payload or {},
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = session.scalars(
            select(TaskRun).where(TaskRun.idempotency_key == idempotency_key)
        ).first()
        return EnqueueResult(created=False, task_id=existing.id if existing else "", task_name=task_name)
    return EnqueueResult(created=True, task_id=row.id, task_name=task_name)


def run_pending(session: Session, *, limit: int = 5) -> RunSummary:
    """领取并执行待办任务。

    「领取」用条件 UPDATE（status='pending' → 'running'）实现：SQLite 的写操作
    本身串行，多进程/多线程下只有一个能把行改成功，天然避免同一任务被重复执行。
    """
    from app.core.config import settings

    summary = RunSummary()
    stmt = (
        select(TaskRun)
        .where(TaskRun.status == "pending")
        .order_by(TaskRun.created_at.asc(), TaskRun.id.asc())
        .limit(limit)
    )
    for row in list(session.scalars(stmt)):
        session.refresh(row)  # 取回最新状态与 updated_at（会话可能持有旧值）
        if not _ready_to_retry(row):
            summary.skipped += 1
            continue

        claimed = session.execute(
            update(TaskRun)
            .where(TaskRun.id == row.id, TaskRun.status == "pending")
            .values(status="running", attempts=TaskRun.attempts + 1)
        ).rowcount
        session.commit()
        if not claimed:
            summary.skipped += 1  # 已被别的 worker 领走
            continue
        summary.claimed += 1

        handler = HANDLERS.get(row.task_name)
        if handler is None:
            _fail(session, row, f"未注册的任务处理器：{row.task_name}", summary)
            continue

        try:
            handler(row.payload or {}, session)
        except Exception as exc:  # 任何异常都不该打断 worker 循环
            session.rollback()
            _fail(session, row, f"{type(exc).__name__}: {exc}", summary)
            continue

        row.status = "succeeded"
        row.last_error = ""
        row.finished_at = _utcnow()
        session.commit()
        summary.succeeded += 1
        summary.ran.append(row.task_name)
        logger.info("task ok name=%s attempts=%d", row.task_name, row.attempts)

    return summary


def _ready_to_retry(row: TaskRun) -> bool:
    """退避判断：第 n 次重试前至少等 `task_retry_backoff_seconds × 2^(n-1)` 秒（上限 60）。

    直接用 updated_at（失败时会被 onupdate 刷新）作为「上次尝试」的时间锚点，
    省掉一个专门的 last_attempt_at 列。退避基数可配置，测试里设为 0 以便即时重试。
    """
    from app.core.config import settings

    if row.attempts == 0:
        return True
    base = settings.task_retry_backoff_seconds
    delay = min(60.0, base * (2 ** (row.attempts - 1)))
    if delay <= 0:
        return True
    last = row.updated_at or row.created_at
    if last is None:
        return True
    if last.tzinfo is not None:
        last = last.replace(tzinfo=None)
    return _utcnow() - last >= timedelta(seconds=delay)


def _fail(session: Session, row: TaskRun, message: str, summary: RunSummary) -> None:
    """失败处理：未耗尽重试次数留在 pending，否则转死信。"""
    row.last_error = message[:1000]
    if row.attempts >= row.max_attempts:
        row.status = "dead"
        row.finished_at = _utcnow()
        summary.dead += 1
        logger.warning("task dead name=%s attempts=%d err=%s", row.task_name, row.attempts, message)
    else:
        row.status = "pending"
        summary.retried += 1
        logger.warning(
            "task retry name=%s attempts=%d/%d err=%s",
            row.task_name, row.attempts, row.max_attempts, message,
        )
    session.commit()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
