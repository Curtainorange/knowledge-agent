"""事件驱动反应：learning_events → 任务/推送的显式映射表（第二阶段 C3）。

设计（与「幂等键表达周期」同构——这里是**幂等键表达已反应**）：

- **拉式扫**：worker 每轮 `ensure_event_reactions` 扫窗口内（`event_reaction_window_hours`）
  的已映射事件类型，不建发布订阅、不建第二套队列、不建 cursor 表；
- **防重账本**：每个事件的反应是一条 `event_reaction` 任务，幂等键
  `react:{event.id}:{event_type}` 由唯一约束挡重——同事件无论被扫到多少次，
  反应只执行一次；反应失败还能借任务框架重试/死信，不用自己造可靠性；
- **显式映射**：`EVENT_REACTIONS` 字典，新增反应 = 加一行 + 一测，宁缺毋滥。

刻意**不映射**的事件（不是遗漏）：

- `L2_JUDGMENT_REVIEWED` / `L2_CONFLICT_FEEDBACK`：消费走**状态表**
  （`conflicts.user_state` 已入推送抑制闸门与弱真值指标）与 L2 弱真值周体检
  （`L2_EVAL_WEEKLY`），不走事件反应——状态表是事实源，事件只是留痕；
- 推送/认证/对话类事件：它们本身就是链路终点，无下游动作。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models.learning_event import LearningEvent
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.feedback import events
from app.workers.tasks import EnqueueResult, enqueue

logger = logging.getLogger(__name__)

TASK_EVENT_REACTION = "event_reaction"

Reaction = Callable[[Session, LearningEvent], None]


def _react_book_digest(session: Session, event: LearningEvent) -> None:
    """通读完成 → 触发一次实时冲突扫描（小时桶合并，天然成本闸门）。"""
    from app.workers.triggers import enqueue_l2_scan

    enqueue_l2_scan(session, user_id=event.user_id, reason="realtime")


def _react_diagnosis(session: Session, event: LearningEvent) -> None:
    """诊断产生 → 即时推送（与月健康推送同 content_hash，互相去重不会双推）。

    事件 payload 只有元数据（诊断正文不入事件），正文回查诊断行。
    """
    from app.agent.push_service import PushService
    from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis

    diagnosis_id = str((event.payload or {}).get("diagnosis_id") or "")
    diagnosis = session.get(CognitiveDiagnosis, diagnosis_id) if diagnosis_id else None
    if diagnosis is None or diagnosis.user_id != event.user_id:
        logger.warning("diagnosis reaction skipped: diagnosis %s not found", diagnosis_id)
        return
    PushService(session).enqueue(
        user_id=event.user_id, push_type="diagnosis",
        title=f"学习健康报告 · {diagnosis.pattern or '诊断'}",
        body=f"{diagnosis.root_cause}\n建议：{diagnosis.suggested_action}",
        subject=diagnosis.id,
    )


EVENT_REACTIONS: dict[str, Reaction] = {
    events.BOOK_DIGEST_DONE: _react_book_digest,
    events.L5_DIAGNOSIS_CREATED: _react_diagnosis,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_event_reactions(session: Session, user_ids: list[str]) -> int:
    """扫描窗口内的已映射事件并逐个排反应任务；返回本轮新入队数。异常只记日志。"""
    if not settings.event_reaction_enabled:
        return 0
    since = _utcnow() - timedelta(hours=max(1, int(settings.event_reaction_window_hours)))
    created = 0
    for user_id in user_ids:
        for event_type in EVENT_REACTIONS:
            try:
                rows = LearningEventRepository(session, user_id=user_id).list_since(
                    user_id, since=since, event_type=event_type, limit=500
                )
                for event in rows:
                    result = enqueue(
                        session,
                        task_name=TASK_EVENT_REACTION,
                        user_id=user_id,
                        payload={"event_id": event.id, "event_type": event.event_type},
                        idempotency_key=f"react:{event.id}:{event_type}",
                    )
                    if result.created:
                        created += 1
            except Exception as exc:  # 反应是旁路，不能反噬 worker 轮询
                logger.warning(
                    "ensure event reactions failed user=%s type=%s err=%s",
                    user_id, event_type, exc,
                )
    return created


def execute_reaction(session: Session, *, event_id: str) -> bool:
    """执行一条事件的反应（由 `event_reaction` 任务处理器调用）。返回是否命中映射。"""
    event = session.get(LearningEvent, event_id)
    if event is None:
        logger.warning("event reaction skipped: event %s not found", event_id)
        return False
    reaction = EVENT_REACTIONS.get(event.event_type)
    if reaction is None:
        return False
    reaction(session, event)
    return True
