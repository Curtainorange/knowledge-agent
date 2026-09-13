"""学习目标 / 计划 / 任务的仓储（L4 路径修正的持久化层）。

三者是一个聚合：目标（做什么）→ 计划（怎么排，带版本号）→ 任务（每周做什么）。
放在一个文件里是因为它们总是一起读写，拆三个文件反而要在三处维护同一套作用域规则。

作用域铁律同其他仓储：写操作必须带 user_id，读操作 `_guard` 校验归属。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.learning_goal import LearningGoal
from app.domain.models.learning_plan import LearningPlan
from app.domain.models.plan_task import PlanTask
from app.domain.repositories.base import BaseRepository


class LearningGoalRepository(BaseRepository[LearningGoal]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        description: str,
        deadline: datetime | None = None,
        priority: str = "medium",
    ) -> LearningGoal:
        goal = LearningGoal(
            user_id=user_id, description=description, deadline=deadline, priority=priority
        )
        self._session.add(goal)
        self._session.flush()
        return goal

    def get(self, goal_id: str) -> LearningGoal | None:
        goal = self._session.get(LearningGoal, goal_id)
        if goal is None:
            return None
        self._guard(goal.user_id)
        return goal

    def list_active(self, user_id: str) -> list[LearningGoal]:
        self._guard(user_id)
        stmt = (
            select(LearningGoal)
            .where(LearningGoal.user_id == user_id, LearningGoal.achieved.is_(False))
            .order_by(LearningGoal.created_at.desc(), LearningGoal.id.asc())
        )
        return list(self._session.scalars(stmt))

    def mark_achieved(self, goal: LearningGoal, achieved: bool = True) -> None:
        goal.achieved = achieved
        self._session.flush()


class LearningPlanRepository(BaseRepository[LearningPlan]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self, *, user_id: str, goal_id: str, content: dict, version: int = 1
    ) -> LearningPlan:
        plan = LearningPlan(user_id=user_id, goal_id=goal_id, content=content, version=version)
        self._session.add(plan)
        self._session.flush()
        return plan

    def get(self, plan_id: str) -> LearningPlan | None:
        plan = self._session.get(LearningPlan, plan_id)
        if plan is None:
            return None
        self._guard(plan.user_id)
        return plan

    def latest_for_goal(self, goal_id: str) -> LearningPlan | None:
        stmt = (
            select(LearningPlan)
            .where(LearningPlan.goal_id == goal_id)
            .order_by(LearningPlan.version.desc())
        )
        return self._session.scalars(stmt).first()

    def latest_for_user(self, user_id: str) -> LearningPlan | None:
        """用户在所有目标里的「最新版本」计划。

        不能按 `created_at desc`——同秒内 `generate_plan` 与 `decide_adjustment`
        会创建 v1 与 v2，时间戳同秒，按时间排会被 SQLite 默认精度写穿、再按 id
        字典序排又会被 UUID 随机性带偏，**版本号才是「最新活动」的稳定判据**。
        """
        self._guard(user_id)
        stmt = (
            select(LearningPlan)
            .where(LearningPlan.user_id == user_id)
            .order_by(LearningPlan.version.desc(), LearningPlan.created_at.desc())
        )
        return self._session.scalars(stmt).first()


class PlanTaskRepository(BaseRepository[PlanTask]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create_many(
        self, *, user_id: str, plan_id: str, tasks: list[dict]
    ) -> list[PlanTask]:
        rows = []
        for task in tasks:
            row = PlanTask(
                user_id=user_id,
                plan_id=plan_id,
                week_index=int(task.get("week_index", 1)),
                subject=str(task.get("subject", ""))[:256],
                status=str(task.get("status", "pending")),
                related_item_ids=list(task.get("related_item_ids") or []),
            )
            self._session.add(row)
            rows.append(row)
        self._session.flush()
        return rows

    def list_by_plan(self, plan_id: str) -> list[PlanTask]:
        stmt = (
            select(PlanTask)
            .where(PlanTask.plan_id == plan_id)
            .order_by(PlanTask.week_index.asc(), PlanTask.created_at.asc())
        )
        return list(self._session.scalars(stmt))

    def set_status(self, task: PlanTask, status: str) -> None:
        task.status = status
        self._session.flush()

    def progress(self, plan_id: str) -> dict:
        """计划完成度：done / total（供偏离检测与界面展示）。"""
        tasks = self.list_by_plan(plan_id)
        done = sum(1 for t in tasks if t.status == "done")
        return {"total": len(tasks), "done": done, "pending": len(tasks) - done}