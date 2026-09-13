"""L2 触发链路：实时（录入后）与周期（周扫），统一走任务队列的幂等入队。

设计要点——**用幂等键表达「合并」与「周期」**，不引入调度器状态：

- 实时：key = `l2:realtime:{user}:{YYYYMMDDHH}`。同一小时内多次录入只排一次扫描，
  避免用户连续收藏 10 篇文章就触发 10 次（每次都要跑 LLM，成本与耗时都不可接受）。
- 周扫：key = `l2:weekly:{user}:{ISO年}-W{ISO周}`。于是「每周扫一次」退化成
  「确保这个 key 存在」——worker 每次轮询都可以无脑调用，天然不会重复执行。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.workers.tasks import EnqueueResult, enqueue

logger = logging.getLogger(__name__)

TASK_L2_SCAN = "l2_scan"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def realtime_key(user_id: str, now: datetime | None = None) -> str:
    stamp = (now or _utcnow()).strftime("%Y%m%d%H")
    return f"l2:realtime:{user_id}:{stamp}"


def weekly_key(user_id: str, now: datetime | None = None) -> str:
    iso = (now or _utcnow()).isocalendar()
    return f"l2:weekly:{user_id}:{iso[0]}-W{iso[1]:02d}"


def enqueue_l2_scan(session: Session, *, user_id: str, reason: str) -> EnqueueResult | None:
    """入队一次 L2 扫描；幂等键按 reason 取不同桶。异常只记日志，绝不影响主链路。"""
    key = realtime_key(user_id) if reason == "realtime" else weekly_key(user_id)
    try:
        result = enqueue(
            session,
            task_name=TASK_L2_SCAN,
            user_id=user_id,
            payload={"user_id": user_id, "reason": reason},
            idempotency_key=key,
        )
    except Exception as exc:  # 排队失败不该让「录入知识」这种主流程失败
        logger.warning("enqueue l2 scan failed user=%s reason=%s err=%s", user_id, reason, exc)
        return None
    if result.created:
        logger.info("l2 scan queued user=%s reason=%s key=%s", user_id, reason, key)
    return result


def trigger_after_ingest(session: Session, *, user_id: str) -> None:
    """录入 / 划词存知识之后的实时触发（受配置开关控制）。"""
    if not settings.l2_realtime_trigger_enabled:
        return
    enqueue_l2_scan(session, user_id=user_id, reason="realtime")


def ensure_weekly_scans(session: Session, user_ids: list[str]) -> int:
    """确保每个活跃用户本周的扫描已排队；返回本轮新入队数。"""
    if not settings.l2_weekly_scan_enabled:
        return 0
    created = 0
    for user_id in user_ids:
        result = enqueue_l2_scan(session, user_id=user_id, reason="weekly")
        if result and result.created:
            created += 1
    return created


# ---- 主动推送调度（ADR-14 的定时侧）-----------------------------------------
# 与 L2 周扫同一套幂等键技巧：周简报 / 月健康报告都退化成「确保这个 key 存在」，
# worker 每轮无脑 ensure 也不会重复推送（PushService 内还有 content_hash 去重兜底）。

TASK_PUSH_WEEKLY = "push_weekly_digest"
TASK_PUSH_MONTHLY = "push_monthly_health"


def push_weekly_key(user_id: str, now: datetime | None = None) -> str:
    iso = (now or _utcnow()).isocalendar()
    return f"push:weekly:{user_id}:{iso[0]}-W{iso[1]:02d}"


def push_monthly_key(user_id: str, now: datetime | None = None) -> str:
    stamp = now or _utcnow()
    return f"push:monthly:{user_id}:{stamp.year}-{stamp.month:02d}"


def enqueue_push(user_id: str, session: Session, *, monthly: bool) -> EnqueueResult | None:
    """入队一次推送任务（周简报 / 月健康）。幂等键按 ISO 周 / 月去重。"""
    key = push_monthly_key(user_id) if monthly else push_weekly_key(user_id)
    try:
        result = enqueue(
            session,
            task_name=TASK_PUSH_MONTHLY if monthly else TASK_PUSH_WEEKLY,
            user_id=user_id,
            payload={"user_id": user_id},
            idempotency_key=key,
        )
    except Exception as exc:
        logger.warning("enqueue push failed user=%s monthly=%s err=%s", user_id, monthly, exc)
        return None
    if result.created:
        logger.info("push queued user=%s monthly=%s key=%s", user_id, monthly, key)
    return result


def ensure_push_schedules(session: Session, user_ids: list[str]) -> int:
    """确保每个活跃用户的周简报与月健康已排队；返回本轮新入队数。"""
    if not settings.push_schedule_enabled:
        return 0
    created = 0
    for user_id in user_ids:
        weekly = enqueue_push(user_id, session, monthly=False)
        monthly = enqueue_push(user_id, session, monthly=True)
        created += (1 if weekly and weekly.created else 0) + (1 if monthly and monthly.created else 0)
    return created