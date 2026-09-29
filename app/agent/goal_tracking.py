"""自主目标追踪（第二阶段 C5）：进度 / 截止 / 达成确认 + 干预未决跟进。

全部是纯函数纯本地规则——**规则能定的不给模型**。产出的提示行进教练周聚合
（`coach.collect_materials`），不单独推送。

铁律：**任何路径都不写 `goal.achieved` / `plan_task.status`**。学习行为 ≠ 任务
完成，自动代判会把目标从 `list_active` 藏掉或把没做的事标成做了——达成与否
只提示用户去确认，重排是 L4 `decide_adjustment` 的职权。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
    PlanTaskRepository,
)
from app.feedback import events


@dataclass
class GoalSnapshot:
    """目标进度快照（只读）。days_to_deadline=None 表示目标没有设截止。"""

    goal_id: str
    description: str
    achieved: bool
    done: int
    total: int
    days_to_deadline: int | None


def goal_snapshot(session: Session, user_id: str, now: datetime | None = None) -> GoalSnapshot | None:
    """取最新未达成目标的进度快照；无目标/无计划返回 None。纯读。"""
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    goals = LearningGoalRepository(session, user_id=user_id).list_active(user_id)
    if not goals:
        return None
    goal = goals[0]  # list_active 已按 created_at desc 排
    plan = LearningPlanRepository(session, user_id=user_id).latest_for_goal(goal.id)
    done, total = 0, 0
    if plan is not None:
        progress = PlanTaskRepository(session).progress(plan.id)
        done, total = progress.get("done", 0), progress.get("total", 0)

    days = None
    if goal.deadline is not None:
        deadline = goal.deadline.replace(tzinfo=None)
        days = (deadline - now).days  # 向下取整：剩 2 天半算 2 天，催办宁早勿晚

    return GoalSnapshot(
        goal_id=goal.id,
        description=goal.description,
        achieved=bool(goal.achieved),
        done=done,
        total=total,
        days_to_deadline=days,
    )


def deadline_nudge(snapshot: GoalSnapshot, *, nudge_days: int = 3) -> str | None:
    """截止临近且未完成 → 催办句；否则 None。已完成的任务不催（别催已经做完的事）。"""
    if snapshot.achieved or snapshot.days_to_deadline is None:
        return None
    if snapshot.days_to_deadline > nudge_days:
        return None
    if snapshot.total and snapshot.done >= snapshot.total:
        return None
    if snapshot.days_to_deadline < 0:
        return f"目标已过期 {abs(snapshot.days_to_deadline)} 天，还有 {snapshot.total - snapshot.done} 项任务未完成"
    return f"距截止只剩 {snapshot.days_to_deadline} 天，还有 {snapshot.total - snapshot.done} 项任务未完成"


def completion_confirm(snapshot: GoalSnapshot) -> str | None:
    """任务全完成但目标未标记达成 → 提示用户去确认。

    **只提示不代判**：不调 `mark_achieved`——任务做完 ≠ 目标达成（比如「学会
    数据库索引」的验收标准可能不在任务清单里），代判会把目标从 list_active 藏掉。
    """
    if snapshot.achieved:
        return None
    if not snapshot.total or snapshot.done < snapshot.total:
        return None
    return "计划任务已全部完成——目标是否达成由你确认（系统不代判）"


def pending_adjustment(session: Session, user_id: str, now: datetime | None = None) -> str | None:
    """最近一次偏离归因后没有调整决定 → 跟进句；已决定则 None。纯读。

    回看窗口取 `l4_intervention_repeat_days`，与巡检归因护栏期对齐——更早的
    未决偏离已经被后续巡检覆盖，翻旧账只会变成每周噪音。
    """
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    since = now - timedelta(days=max(1, int(settings.l4_intervention_repeat_days)))
    event_repo = LearningEventRepository(session, user_id=user_id)

    checked = [
        e for e in event_repo.list_recent(user_id, event_type=events.L4_DEVIATION_CHECKED, limit=10)
        if e.occurred_at and e.occurred_at >= since
    ]
    if not checked:
        return None
    latest = checked[0]  # list_recent 按 occurred_at desc
    decided = [
        e for e in event_repo.list_recent(user_id, event_type=events.L4_ADJUSTMENT_DECIDED, limit=10)
        if e.occurred_at and latest.occurred_at and e.occurred_at >= latest.occurred_at
    ]
    if decided:
        return None
    return "上次的计划偏离建议还未决定——别让归因白做"
