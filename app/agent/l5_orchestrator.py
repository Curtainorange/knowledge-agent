"""L5 归因式「学习偏误诊断」编排（系统设计 §5.5 / 需求 UC-L5-01、UC-L5-02）。

目标：不止展示行为统计，更链式推理出行为背后的认知病根，给可执行方案。

与 L4 同一条原则——**信号在代码里算，模型只做归因**：

1. 行为指标（收藏完成比 / 活跃度 / 连续无活动天数 / 未处理冲突数）全部本地统计，
   模型不参与算术（模型算错一个数，整份诊断的可信度就归零）。
2. 只有归因这一步才走 `causal_reasoning`（reasoning=on），产出 pattern / root_cause /
   suggested_action / reasoning_chain。
3. **置信度校准（ADR-15）**：不再信任模型的裸 confidence（0.5 硬阈值），而是用
   「数据充分度」缩放 + 「信号一致性」微调——行为样本太少时，即便模型很自信，
   诊断依据也不足，校准后会打折。

闭环：诊断生成 → 用户可采纳 / 拒绝；采纳后回写计划系统（记录采纳意图，供 L4 后续
重规划参考），拒绝则记录偏好（L5 会「用户曾拒绝过什么」避免重复打扰）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.cognitive_diagnosis_repository import CognitiveDiagnosisRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.feedback import events
from app.llm.gateway import ModelGateway
from app.llm.prompts import L5_DIAGNOSE
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

# 计入「学习行为」的事件类型（与 L4 一致，是计划执行与活跃度的证据）
_STUDY_EVENTS = (
    events.KNOWLEDGE_CREATED,
    events.NOTE_CREATED,
    events.BOOK_PROGRESS,
    events.L1_MINE,
)


class DiagnosisDraft(BaseModel):
    pattern: str = Field(default="", max_length=64)
    root_cause: str = Field(default="", max_length=2000)
    suggested_action: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reasoning_chain: list[str] = Field(default_factory=list)


@dataclass
class BehaviorMetrics:
    """行为指标（全部本地统计，模型不参与算术）。"""

    total_items: int = 0
    completed_items: int = 0
    completion_ratio: float = 0.0
    study_events_week: int = 0
    idle_days: int | None = None
    unseen_conflicts: int = 0

    @property
    def signal_count(self) -> int:
        """「有问题」的信号数量，用于置信度校准的一致性微调。"""
        n = 0
        if self.completion_ratio < 0.3 and self.total_items >= 5:
            n += 1
        if self.idle_days is not None and self.idle_days >= settings.l4_idle_days_threshold:
            n += 1
        if self.unseen_conflicts >= 1:
            n += 1
        return n


@dataclass
class L5Result:
    state: Literal["ok", "empty", "degraded"]
    diagnosis_id: str = ""
    pattern: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    suggested_action: str = ""
    reasoning_chain: list[str] = field(default_factory=list)
    metrics: BehaviorMetrics | None = None
    note: str = ""


def calibrate_confidence(model_confidence: float, *, event_count: int, signal_count: int) -> float:
    """置信度校准（ADR-15）：数据充分度缩放 + 信号一致性微调。

    - 数据充分度：行为样本越多越可信；样本 < 20 条按比例打折，保底 0.3。
    - 信号一致性：多个独立信号指向同一结论时小幅上调（上限 +0.1）。
    校准把「模型自信」与「证据充分」分开——模型说 0.9 但只有 2 条行为数据，
    校准后不足 0.5，才不至于被一次低质量归因带偏。
    """
    support = min(1.0, max(0.3, event_count / 20.0))
    consistency_bonus = min(0.1, 0.02 * max(0, signal_count - 1))
    raw = float(model_confidence) * support + consistency_bonus
    return round(min(1.0, max(0.0, raw)), 3)


class L5Orchestrator:
    def __init__(self, gateway: ModelGateway | None = None, session: Session = None):
        self._gateway = gateway
        self._session = session
        self._repo = CognitiveDiagnosisRepository(session)

    @property
    def gateway(self) -> ModelGateway:
        if self._gateway is None:
            self._gateway = ModelGateway()
        return self._gateway

    # ---- UC-L5-01 / UC-L5-02 归因诊断 --------------------------------------

    def diagnose(self, *, user_id: str) -> L5Result:
        metrics = self._compute_metrics(user_id)
        if metrics.total_items == 0 and metrics.study_events_week == 0:
            return L5Result(state="empty", metrics=metrics, note="还没有足够的学习行为，先积累一些再来诊断吧。")

        draft = self._attribute(metrics, user_id=user_id)
        if draft is None:
            return L5Result(
                state="degraded", metrics=metrics, note="归因分析失败（模型输出无法解析），请稍后重试。"
            )

        confidence = calibrate_confidence(
            draft.confidence,
            event_count=metrics.study_events_week,
            signal_count=metrics.signal_count,
        )
        diagnosis = self._repo.create(
            user_id=user_id,
            pattern=draft.pattern,
            root_cause=draft.root_cause,
            confidence=confidence,
            suggested_action=draft.suggested_action,
            reasoning_chain=draft.reasoning_chain,
        )
        self._session.commit()
        events.record(
            self._session, user_id=user_id, event_type=events.L5_DIAGNOSIS_CREATED,
            payload={"diagnosis_id": diagnosis.id, "pattern": draft.pattern,
                     "confidence": confidence, "raw_confidence": draft.confidence},
        )
        return L5Result(
            state="ok",
            diagnosis_id=diagnosis.id,
            pattern=diagnosis.pattern,
            root_cause=diagnosis.root_cause,
            confidence=diagnosis.confidence,
            suggested_action=diagnosis.suggested_action,
            reasoning_chain=list(diagnosis.reasoning_chain or []),
            metrics=metrics,
        )

    def decide(self, *, user_id: str, diagnosis_id: str, accepted: bool) -> tuple[bool, str]:
        """用户对诊断的采纳/拒绝。采纳 → 回写计划（闭环）；拒绝 → 记偏好。"""
        diagnosis = self._repo.get(diagnosis_id)
        if diagnosis is None or diagnosis.user_id != user_id:
            return False, "诊断不存在"
        self._repo.set_status(diagnosis, "accepted" if accepted else "rejected")
        self._session.commit()
        events.record(
            self._session, user_id=user_id, event_type=events.L5_DIAGNOSIS_DECIDED,
            payload={"diagnosis_id": diagnosis_id, "accepted": accepted,
                     "pattern": diagnosis.pattern},
        )
        if accepted:
            if diagnosis.suggested_action:
                self._append_to_plan(user_id, diagnosis)
                return True, "已采纳诊断，建议已加入你的学习计划"
            return True, "已采纳诊断（本诊断无具体建议，未写入计划）"
        return True, "已记录你的判断，后续会减少同类打扰"

    def _append_to_plan(self, user_id: str, diagnosis) -> None:
        """把诊断建议追加为学习计划的一个任务（形成闭环：诊断 → 计划 → 执行）。

        没有现存计划时先建一个「采纳诊断建议」的目标与计划，再挂任务——保证采纳
        建议总有落点，而不是因「用户还没建计划」而静默丢弃。
        """
        from app.domain.repositories.learning_plan_repository import (
            LearningGoalRepository,
            LearningPlanRepository,
            PlanTaskRepository,
        )

        plan_repo = LearningPlanRepository(self._session, user_id=user_id)
        plan = plan_repo.latest_for_user(user_id)
        if plan is None:
            goal = LearningGoalRepository(self._session, user_id=user_id).create(
                user_id=user_id,
                description=f"采纳诊断建议：{diagnosis.pattern or '学习调整'}",
            )
            plan = plan_repo.create(
                user_id=user_id, goal_id=goal.id,
                content={"rationale": f"来自 L5 诊断 {diagnosis.id}"},
                version=1,
            )
        task_repo = PlanTaskRepository(self._session, user_id=user_id)
        existing = task_repo.list_by_plan(plan.id)
        next_week = max([t.week_index for t in existing] + [0]) + 1
        task_repo.create_many(
            user_id=user_id, plan_id=plan.id,
            tasks=[{
                "week_index": next_week,
                "subject": (diagnosis.suggested_action or "")[:256],
                "status": "pending",
            }],
        )
        self._session.commit()

    def latest(self, *, user_id: str):
        return self._repo.latest_for_user(user_id)

    # ---- 内部：本地行为指标 ------------------------------------------------

    def _compute_metrics(self, user_id: str) -> BehaviorMetrics:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        week_start = now - timedelta(days=7)

        items = KnowledgeRepository(self._session, user_id=user_id).list_active(user_id)
        total = len(items)
        completed = sum(1 for i in items if (i.read_progress or 0.0) >= 1.0)

        event_repo = LearningEventRepository(self._session, user_id=user_id)
        history = event_repo.list_recent(user_id, limit=1000)
        study = [e for e in history if e.event_type in _STUDY_EVENTS]
        week_study = [e for e in study if e.occurred_at and e.occurred_at >= week_start]
        last_at = max((e.occurred_at for e in study if e.occurred_at), default=None)
        idle_days = None if last_at is None else (now - last_at).days

        unseen = ConflictRepository(self._session, user_id=user_id).count_by_state(user_id).get(
            "unseen", 0
        )

        return BehaviorMetrics(
            total_items=total,
            completed_items=completed,
            completion_ratio=(completed / total) if total else 0.0,
            study_events_week=len(week_study),
            idle_days=idle_days,
            unseen_conflicts=unseen,
        )

    def _attribute(self, metrics: BehaviorMetrics, *, user_id: str) -> DiagnosisDraft | None:
        prompt = (
            f"行为统计（本地计算，事实）：\n"
            f"- 知识库共 {metrics.total_items} 条，已读完 {metrics.completed_items} 条"
            f"（收藏完成比 {metrics.completion_ratio:.0%}）\n"
            f"- 本周学习行为 {metrics.study_events_week} 次\n"
            f"- 距上一次学习行为："
            f"{'窗口内无记录' if metrics.idle_days is None else str(metrics.idle_days) + ' 天'}\n"
            f"- 未处理观点冲突 {metrics.unseen_conflicts} 处\n"
        )
        try:
            completion = self.gateway.chat(
                task_type="causal_reasoning",
                messages=[{"role": "system", "content": L5_DIAGNOSE.text},
                          {"role": "user", "content": prompt}],
                user_id=user_id,
                session=self._session,
                prompt_version=L5_DIAGNOSE.version,
            )
            return parse_structured(completion.text, validator=lambda d: DiagnosisDraft(**d))
        except JsonParseError as exc:
            logger.warning("l5 diagnosis failed: %s", exc)
            return None
