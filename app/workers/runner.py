"""任务 worker：单进程内的守护线程轮询（不依赖 Celery/Redis/APScheduler）。

每轮做两件事：
1. 确保「本周扫描」已排队（幂等键按 ISO 周去重，重复调用不会重复执行）
2. 领取并执行待办任务（条件 UPDATE 领取，避免重复执行）

关闭方式：`settings.worker_enabled=False`（测试与只读场景）；线程为 daemon，
进程退出即终止，无需优雅停机逻辑。
"""
from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings
from app.domain.db import SessionLocal
from app.domain.repositories.user_repository import UserRepository
from app.workers import handlers  # noqa: F401  导入即注册任务处理器
from app.workers.tasks import RunSummary, run_pending
from app.workers.triggers import ensure_push_schedules, ensure_weekly_scans

logger = logging.getLogger(__name__)

_started = False
_start_lock = threading.Lock()


def run_once(session) -> RunSummary:
    """跑一轮（供测试与脚本手动调用，不起线程）。"""
    user_ids = UserRepository(session).list_ids()
    created = ensure_weekly_scans(session, user_ids)
    if created:
        logger.info("weekly l2 scans queued: %d", created)
    pushed = ensure_push_schedules(session, user_ids)
    if pushed:
        logger.info("push schedules queued: %d", pushed)
    return run_pending(session)


def start_worker() -> bool:
    """启动守护线程；已启动或已禁用时返回 False。"""
    global _started
    if not settings.worker_enabled:
        logger.info("worker disabled by config (worker_enabled=False)")
        return False
    with _start_lock:
        if _started:
            return False
        _started = True
    thread = threading.Thread(target=_loop, name="cc-task-worker", daemon=True)
    thread.start()
    logger.info("task worker started (poll=%.1fs)", settings.worker_poll_seconds)
    return True


def _loop() -> None:
    while True:
        try:
            session = SessionLocal()
            try:
                summary = run_once(session)
                if summary.claimed:
                    logger.info(
                        "task round: claimed=%d ok=%d retry=%d dead=%d",
                        summary.claimed, summary.succeeded, summary.retried, summary.dead,
                    )
            finally:
                session.close()
        except Exception as exc:  # 任何异常都不能让轮询线程死掉
            logger.warning("task worker round failed: %s", exc)
        time.sleep(max(1.0, settings.worker_poll_seconds))