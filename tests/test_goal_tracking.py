"""自主目标追踪测试（第二阶段 C5）：进度/截止/达成确认 + 干预未决跟进。

重点：
1. 快照与催办边界（nudge_days 内才催，做完的不催）；
2. 达成**只提示不代判**——goal.achieved / plan_task.status 永不被自动改写；
3. 未决跟进：偏离归因后没有调整决定才催，已决定不催。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.agent import goal_tracking
from app.agent.coach import collect_materials
from app.agent.goal_tracking import (
    GoalSnapshot,
    completion_confirm,
    deadline_nudge,
    goal_snapshot,
    pending_adjustment,
)
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
    PlanTaskRepository,
)
from app.feedback import events


def _seed_plan(session, *, deadline: datetime | None, statuses: list[str]) -> str:
    goal = LearningGoalRepository(session, user_id="u1").create(
        user_id="u1", description="学会数据库索引", deadline=deadline,
    )
    plan = LearningPlanRepository(session, user_id="u1").create(
        user_id="u1", goal_id=goal.id, content={},
    )
    PlanTaskRepository(session, user_id="u1").create_many(
        user_id="u1", plan_id=plan.id,
        tasks=[{"week_index": i + 1, "subject": f"任务{i + 1}", "status": s}
               for i, s in enumerate(statuses)],
    )
    session.commit()
    return goal.id


def _emit(session, event_type: str, hours_ago: float = 0.0) -> None:
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    LearningEventRepository(session).append(
        user_id="u1", event_type=event_type, payload={"reasons": ["idle_days=5"]}, occurred_at=when,
    )
    session.commit()


# ---------- 快照与催办边界 ----------


def test_goal_snapshot_reports_progress_and_deadline(session):
    deadline = datetime.now(timezone.utc) + timedelta(days=2)
    _seed_plan(session, deadline=deadline, statuses=["done", "done", "pending"])

    snap = goal_snapshot(session, "u1")

    assert snap is not None
    assert (snap.done, snap.total) == (2, 3)
    assert snap.days_to_deadline is not None and snap.days_to_deadline <= 2
    assert snap.achieved is False


def test_deadline_nudge_boundary():
    def snap(days: int, done: int = 1, total: int = 3) -> GoalSnapshot:
        return GoalSnapshot(goal_id="g", description="d", achieved=False,
                            done=done, total=total, days_to_deadline=days)

    assert deadline_nudge(snap(3), nudge_days=3) is not None  # 边界内催
    assert deadline_nudge(snap(4), nudge_days=3) is None      # 边界外不催
    assert deadline_nudge(snap(1, done=3), nudge_days=3) is None  # 做完的不催
    assert deadline_nudge(snap(-1), nudge_days=3) is not None  # 过期也提醒


# ---------- 达成只提示不代判 ----------


def test_completion_confirm_prompts_but_never_marks(session):
    goal_id = _seed_plan(session, deadline=None, statuses=["done", "done"])
    snap = goal_snapshot(session, "u1")

    assert completion_confirm(snap) is not None  # 提示用户确认

    materials = collect_materials(session, "u1")  # 走一遍教练聚合（最大自动化路径）
    goal = LearningGoalRepository(session, user_id="u1").get(goal_id)
    assert goal.achieved is False  # 铁律：不写 goal.achieved
    tasks = PlanTaskRepository(session).list_by_plan(
        LearningPlanRepository(session, user_id="u1").latest_for_goal(goal_id).id
    )
    assert [t.status for t in tasks] == ["done", "done"]  # 任务状态也不被改写
    assert any(b.next_step for b in materials.blocks)


def test_completion_confirm_silent_while_tasks_remain():
    snap = GoalSnapshot(goal_id="g", description="d", achieved=False, done=1, total=3, days_to_deadline=None)
    assert completion_confirm(snap) is None
    snap_done = GoalSnapshot(goal_id="g", description="d", achieved=True, done=3, total=3, days_to_deadline=None)
    assert completion_confirm(snap_done) is None  # 已达成不用再确认


# ---------- 干预未决跟进 ----------


def test_pending_adjustment_follows_up_undecided(session):
    _emit(session, events.L4_DEVIATION_CHECKED, hours_ago=1)  # 归因了
    assert pending_adjustment(session, "u1") is not None       # 但没人决定 → 跟进


def test_pending_adjustment_silent_once_decided(session):
    _emit(session, events.L4_DEVIATION_CHECKED, hours_ago=2)
    _emit(session, events.L4_ADJUSTMENT_DECIDED, hours_ago=1)  # 决定发生在归因之后
    assert pending_adjustment(session, "u1") is None


def test_pending_adjustment_ignores_old_deviations(session):
    _emit(session, events.L4_DEVIATION_CHECKED, hours_ago=24 * 10)  # 10 天前的旧账
    assert pending_adjustment(session, "u1") is None
