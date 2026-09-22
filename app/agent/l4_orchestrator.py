"""L4 路径修正编排（系统设计 §5.4 / 需求 UC-L4-01、UC-L4-02）。

三段闭环：

1. **定目标 → 拆计划**（UC-L4-01）：目标描述 + 知识结构上下文
   （复用 L2 已抽取的主张主题，**不再额外调模型**）→ `plan_generation` 出周维度任务
2. **行为监测 → 归因**（UC-L4-02）：本地算出偏离信号（连续无学习行为 / 活动量突变 /
   计划完成度停滞 / 新内容与计划主题脱节）→ `deep_reasoning`（reasoning=on）做归因
   与调整建议
3. **询问 → 应用或记录偏好**：用户同意才重拆计划（版本 +1）；拒绝则保持原计划并把
   这次偏好记进事件表（L5 诊断会用到「用户曾拒绝过什么」）

三个刻意的工程取舍：

- **监测信号在代码里算，模型只做归因**：连续几天没动、活动量涨了几倍这类事实必须由
  本地统计得出（与 L3 的「别让模型做算术」同一条原则），模型负责解释「为什么」与
  「怎么办」——这正是它擅长的部分。
- **监测不入模型主链路**（设计要点）：`check_deviation` 先只做本地统计，只有确认存在
  偏离时才调模型。否则每次轮询都是一次白花的调用。
- **计划只能经单一出口改写**：`_apply_plan_change` 是唯一写计划内容的地方，扮演设计
  文档里 `update_plan` 工具的角色（P0 不发起真实 Function Calling，那需要工具执行
  循环与动作审计；先把「受控改写」的结构立住）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
    PlanTaskRepository,
)
from app.feedback import events
from app.llm.gateway import ModelGateway
from app.llm.prompts import L4_DEVIATE, L4_PLAN
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

# 计入「学习行为」的事件类型：这些是计划执行的证据
_STUDY_EVENTS = (
    events.KNOWLEDGE_CREATED,
    events.NOTE_CREATED,
    events.BOOK_PROGRESS,
    events.L1_MINE,
)


class GeneratedTask(BaseModel):
    week_index: int = Field(ge=1, le=52)
    subject: str = Field(min_length=1, max_length=256)
    focus: str = ""                       # 这一周要解决什么
    related_item_ids: list[str] = Field(default_factory=list)


class GeneratedPlan(BaseModel):
    tasks: list[GeneratedTask] = Field(default_factory=list)
    rationale: str = ""


class DeviationAnalysis(BaseModel):
    root_cause: str = ""
    adjustment: str = ""
    expected_gain: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# 提示词统一在 app/llm/prompts.py 声明（版本化 + golden set 校验），此处仅取别名
_PLAN_SYS = L4_PLAN.text
_DEVIATE_SYS = L4_DEVIATE.text


@dataclass
class DeviationSignals:
    """偏离信号（全部来自本地统计，不含模型判断）。"""

    window_days: int
    idle_days: int | None          # 距上一次学习行为的天数（None = 窗口内无记录）
    recent_events: int
    previous_events: int
    recent_new_items: int
    previous_new_items: int
    plan_total: int
    plan_done: int
    topic_overlap: float           # 近期新增标题与计划任务主题的词面重合度 0..1
    reasons: list[str] = field(default_factory=list)

    @property
    def has_deviation(self) -> bool:
        return bool(self.reasons)


@dataclass
class L4DeviationReport:
    state: Literal["ok", "no_plan", "no_deviation", "degraded"]
    signals: DeviationSignals | None = None
    analysis: DeviationAnalysis | None = None
    plan_id: str = ""
    note: str = ""


@dataclass
class L4PlanView:
    goal_id: str
    goal_description: str
    plan_id: str
    version: int
    rationale: str
    tasks: list[dict]
    progress: dict


@dataclass
class L4PlanResult:
    """计划生成结果。

    刻意区分「目标不存在」与「生成失败」：前者是 404，后者是**降级**
    （目标在、只是模型这次没给出可用计划）。若把两者混在一起，调用方会收到
    「目标不存在」这种与事实相反的提示。
    """

    state: Literal["ok", "degraded"]
    view: L4PlanView | None
    note: str = ""


class L4Orchestrator:
    def __init__(self, gateway: ModelGateway | None = None, session: Session = None):
        # gateway 可空：建目标、查计划、看目标列表都不需要模型；
        # 惰性构造避免「传了 None 又走到模型调用」时炸在 AttributeError 上
        self._gateway = gateway
        self._session = session
        self._goals = LearningGoalRepository(session)
        self._plans = LearningPlanRepository(session)
        self._tasks = PlanTaskRepository(session)

    @property
    def gateway(self) -> ModelGateway:
        if self._gateway is None:
            self._gateway = ModelGateway()
        return self._gateway

    # ---- UC-L4-01 定目标 → 拆计划 -----------------------------------------

    def create_goal(
        self, *, user_id: str, description: str, deadline: datetime | None = None, priority: str = "medium"
    ) -> str:
        goal = LearningGoalRepository(self._session, user_id=user_id).create(
            user_id=user_id, description=description, deadline=deadline, priority=priority
        )
        self._session.commit()
        events.record(
            self._session, user_id=user_id, event_type=events.L4_GOAL_CREATED,
            payload={"goal_id": goal.id, "priority": priority, "has_deadline": deadline is not None},
        )
        return goal.id

    def generate_plan(self, *, user_id: str, goal_id: str) -> L4PlanResult | None:
        """为某个目标生成计划。目标不存在返回 None（404）；生成失败返回 degraded。"""
        goal = LearningGoalRepository(self._session, user_id=user_id).get(goal_id)
        if goal is None:
            return None

        context = self._knowledge_context(user_id)
        item_ids = [i.id for i in KnowledgeRepository(self._session, user_id=user_id).list_active(user_id)][:60]
        prompt = (
            f"学习目标：{goal.description}\n"
            f"优先级：{goal.priority}；"
            f"截止：{goal.deadline.strftime('%Y-%m-%d') if goal.deadline else '未指定'}\n"
            f"可关联的条目 id 清单：{', '.join(item_ids) if item_ids else '（知识库为空）'}\n\n"
            f"{context}"
        )
        draft = self._generate_tasks(prompt, user_id=user_id)
        if draft is None:
            existing = self._latest_view(goal_id)
            return L4PlanResult(
                state="degraded",
                view=existing,
                note="计划生成失败（模型输出无法解析），已保留既有计划，请重试。",
            )

        plan = self._apply_plan_change(
            user_id=user_id, goal_id=goal_id,
            content={"rationale": draft.rationale},
            tasks=[t.model_dump() for t in draft.tasks[: settings.l4_max_tasks]],
            version=self._next_version(goal_id),
        )
        events.record(
            self._session, user_id=user_id, event_type=events.L4_PLAN_GENERATED,
            payload={"goal_id": goal_id, "plan_id": plan.id, "version": plan.version,
                     "tasks": len(draft.tasks)},
        )
        return L4PlanResult(state="ok", view=self._view(goal_id, plan))

    # ---- UC-L4-02 偏离监测 → 归因 -----------------------------------------

    def check_deviation(self, *, user_id: str) -> L4DeviationReport:
        plan = LearningPlanRepository(self._session, user_id=user_id).latest_for_user(user_id)
        if plan is None:
            return L4DeviationReport(state="no_plan", note="还没有学习计划，先设定目标并生成计划。")

        signals = self._compute_signals(user_id, plan)
        if not signals.has_deviation:
            return L4DeviationReport(
                state="no_deviation", signals=signals, plan_id=plan.id,
                note="计划执行正常，暂无需干预。",
            )

        analysis = self._analyze(signals, plan, user_id=user_id)
        if analysis is None:
            return L4DeviationReport(
                state="degraded", signals=signals, plan_id=plan.id,
                note="检测到偏离，但归因分析失败（模型输出无法解析）。",
            )

        events.record(
            self._session, user_id=user_id, event_type=events.L4_DEVIATION_CHECKED,
            payload={
                "plan_id": plan.id, "reasons": signals.reasons,
                "idle_days": signals.idle_days,
                "confidence": analysis.confidence,
            },
        )
        return L4DeviationReport(
            state="ok", signals=signals, analysis=analysis, plan_id=plan.id,
            note="检测到偏离，以下是归因与调整建议。",
        )

    def decide_adjustment(
        self, *, user_id: str, plan_id: str, accepted: bool
    ) -> tuple[bool, str]:
        """用户对调整建议的决定。同意 → 重拆计划（版本 +1）；拒绝 → 记录偏好。"""
        plan = LearningPlanRepository(self._session, user_id=user_id).get(plan_id)
        if plan is None:
            return False, "计划不存在"

        events.record(
            self._session, user_id=user_id, event_type=events.L4_ADJUSTMENT_DECIDED,
            payload={"plan_id": plan_id, "accepted": accepted, "version": plan.version},
        )
        if not accepted:
            return True, "已保持原计划，并记录你的偏好（后续会减少同类建议）"

        goal = LearningGoalRepository(self._session, user_id=user_id).get(plan.goal_id)
        if goal is None:
            return False, "计划关联的目标不存在"

        context = self._knowledge_context(user_id)
        prompt = (
            f"学习目标：{goal.description}\n"
            f"上一版计划被打断，请重新拆解（可调整顺序、切分粒度与单周负荷）：\n"
            f"{self._plan_digest(plan_id)}\n\n{context}"
        )
        draft = self._generate_tasks(prompt, user_id=user_id)
        if draft is None:
            return False, "重规划失败（模型输出无法解析），已保留原计划"

        self._apply_plan_change(
            user_id=user_id, goal_id=plan.goal_id,
            content={"rationale": draft.rationale, "replanned_from": plan.id},
            tasks=[t.model_dump() for t in draft.tasks[: settings.l4_max_tasks]],
            version=plan.version + 1,
        )
        return True, "已按建议重排计划"

    # ---- 查询 --------------------------------------------------------------

    def latest_plan_view(self, *, user_id: str) -> L4PlanView | None:
        plan = LearningPlanRepository(self._session, user_id=user_id).latest_for_user(user_id)
        if plan is None:
            return None
        return self._view(plan.goal_id, plan)

    def list_goals(self, *, user_id: str) -> list[dict]:
        goals = LearningGoalRepository(self._session, user_id=user_id).list_active(user_id)
        return [
            {
                "goal_id": g.id,
                "description": g.description,
                "priority": g.priority,
                "deadline": g.deadline,
                "achieved": g.achieved,
            }
            for g in goals
        ]

    # ---- 内部：受控改写 ----------------------------------------------------

    def _apply_plan_change(self, *, user_id: str, goal_id: str, content: dict, tasks: list[dict], version: int):
        """**唯一**改写计划内容的出口（扮演设计文档里的 update_plan 工具）。

        新版本 = 新增一行 plan（保留历史版本便于回溯被推翻的路径），任务整体重建。
        """
        repo = LearningPlanRepository(self._session, user_id=user_id)
        plan = repo.create(user_id=user_id, goal_id=goal_id, content=content, version=version)
        PlanTaskRepository(self._session, user_id=user_id).create_many(
            user_id=user_id, plan_id=plan.id, tasks=tasks
        )
        self._session.commit()
        return plan

    def _next_version(self, goal_id: str) -> int:
        latest = LearningPlanRepository(self._session).latest_for_goal(goal_id)
        return (latest.version + 1) if latest else 1

    # ---- 内部：知识结构上下文（复用 L2 主张，零额外模型调用）------------------

    def _knowledge_context(self, user_id: str) -> str:
        claims = ClaimRepository(self._session, user_id=user_id).list_by_user(user_id)
        topic_counts: dict[str, int] = {}
        for claim in claims:
            topic = (claim.topic or "").strip()
            if topic:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
        topics = sorted(topic_counts.items(), key=lambda kv: -kv[1])[:8]
        items = KnowledgeRepository(self._session, user_id=user_id).list_active(user_id)[:15]

        lines = []
        if topics:
            lines.append("已有知识主题（来自主张抽取）：" + "、".join(f"{t}×{c}" for t, c in topics))
        else:
            lines.append("已有知识主题：暂无（尚未做过 L2 主张抽取）")
        if items:
            lines.append("近期条目标题：\n" + "\n".join(f"- {i.title}" for i in items))
        return "\n".join(lines)

    def _plan_digest(self, plan_id: str) -> str:
        tasks = PlanTaskRepository(self._session).list_by_plan(plan_id)
        if not tasks:
            return "（上一版计划没有任务）"
        return "\n".join(f"- 第 {t.week_index} 周：{t.subject}（{t.status}）" for t in tasks)

    # ---- 内部：模型调用 ----------------------------------------------------

    def _generate_tasks(self, prompt: str, *, user_id: str) -> GeneratedPlan | None:
        try:
            completion = self.gateway.chat(
                task_type="plan_generation",
                messages=[{"role": "system", "content": _PLAN_SYS},
                          {"role": "user", "content": prompt}],
                user_id=user_id,
                session=self._session,
                prompt_version=L4_PLAN.version,
                json_model=GeneratedPlan,
            )
            return parse_structured(completion.text, validator=lambda d: GeneratedPlan(**d))
        except JsonParseError as exc:
            logger.warning("l4 plan generation failed: %s", exc)
            return None

    def _analyze(self, signals: DeviationSignals, plan, *, user_id: str):
        goal = LearningGoalRepository(self._session, user_id=plan.user_id).get(plan.goal_id)
        prompt = (
            f"学习目标：{goal.description if goal else '（已删除）'}\n"
            f"计划版本：v{plan.version}；任务进度：{signals.plan_done}/{signals.plan_total} 已完成\n"
            f"计划内容：\n{self._plan_digest(plan.id)}\n\n"
            f"行为统计（本地统计，事实）：\n"
            f"- 统计窗口：最近 {signals.window_days} 天\n"
            f"- 距上一次学习行为："
            f"{'窗口内无任何学习行为' if signals.idle_days is None else str(signals.idle_days) + ' 天'}\n"
            f"- 学习行为次数：本窗口 {signals.recent_events} 次，上一窗口 {signals.previous_events} 次\n"
            f"- 新增知识条目：本窗口 {signals.recent_new_items} 条，上一窗口 {signals.previous_new_items} 条\n"
            f"- 近期新增内容与计划主题的重合度：{signals.topic_overlap:.0%}\n"
            f"- 已触发的偏离信号：{'；'.join(signals.reasons)}\n"
        )
        try:
            completion = self.gateway.chat(
                task_type="deep_reasoning",
                messages=[{"role": "system", "content": _DEVIATE_SYS},
                          {"role": "user", "content": prompt}],
                user_id=user_id,
                session=self._session,
                prompt_version=L4_DEVIATE.version,
                json_model=DeviationAnalysis,
            )
            return parse_structured(completion.text, validator=lambda d: DeviationAnalysis(**d))
        except JsonParseError as exc:
            logger.warning("l4 deviation analysis failed: %s", exc)
            return None

    # ---- 内部：本地统计（偏离信号的唯一来源）--------------------------------

    def _compute_signals(self, user_id: str, plan) -> DeviationSignals:
        window = settings.l4_window_days
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        recent_start = now - timedelta(days=window)
        previous_start = now - timedelta(days=window * 2)

        event_repo = LearningEventRepository(self._session, user_id=user_id)
        history = event_repo.list_recent(user_id, limit=1000)
        study = [e for e in history if e.event_type in _STUDY_EVENTS]
        recent = [e for e in study if e.occurred_at and e.occurred_at >= recent_start]
        previous = [
            e for e in study if e.occurred_at and previous_start <= e.occurred_at < recent_start
        ]

        last_at = max((e.occurred_at for e in study if e.occurred_at), default=None)
        idle_days = None if last_at is None else (now - last_at).days

        def _items_since(start, end=None) -> int:
            rows = event_repo.list_recent(user_id, event_type=events.KNOWLEDGE_CREATED, limit=1000)
            return sum(
                1 for r in rows
                if r.occurred_at and r.occurred_at >= start and (end is None or r.occurred_at < end)
            )

        recent_items = _items_since(recent_start)
        previous_items = _items_since(previous_start, recent_start)

        progress = PlanTaskRepository(self._session).progress(plan.id)
        overlap = self._topic_overlap(user_id, plan.id)

        reasons: list[str] = []
        if idle_days is None or idle_days >= settings.l4_idle_days_threshold:
            reasons.append(
                "连续 {} 天没有学习行为".format(
                    idle_days if idle_days is not None else settings.l4_idle_days_threshold
                )
            )
        if recent_events_drop(len(previous), len(recent)):
            reasons.append(f"学习行为较上一窗口下降（{len(previous)} → {len(recent)} 次）")
        if previous_items and recent_items >= previous_items * settings.l4_spike_factor:
            reasons.append(f"新增内容突增（上一窗口 {previous_items} → 本窗口 {recent_items} 条）")
        if progress["total"] and recent and overlap < 0.2:
            reasons.append("近期新增内容与计划主题基本无关（重合度低于 20%），目标可能已转移")

        return DeviationSignals(
            window_days=window,
            idle_days=idle_days,
            recent_events=len(recent),
            previous_events=len(previous),
            recent_new_items=recent_items,
            previous_new_items=previous_items,
            plan_total=progress["total"],
            plan_done=progress["done"],
            topic_overlap=overlap,
            reasons=reasons,
        )

    def _topic_overlap(self, user_id: str, plan_id: str) -> float:
        """近期新增条目标题与计划任务主题的字符二元组重合度（0..1，纯本地）。"""
        tasks = PlanTaskRepository(self._session).list_by_plan(plan_id)
        subjects = "".join(t.subject for t in tasks)
        if not subjects:
            return 1.0

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        since = now - timedelta(days=settings.l4_window_days)
        items = KnowledgeRepository(self._session, user_id=user_id).list_active(user_id)
        recent_titles = "".join(
            i.title for i in items if i.created_at and i.created_at >= since
        )
        if not recent_titles:
            return 1.0

        def grams(text: str) -> set[str]:
            return {text[i : i + 2] for i in range(max(0, len(text) - 1))}

        subject_grams = grams(subjects)
        if not subject_grams:
            return 1.0
        return len(grams(recent_titles) & subject_grams) / len(subject_grams)

    # ---- 内部：视图组装 ----------------------------------------------------

    def _view(self, goal_id: str, plan) -> L4PlanView:
        goal = LearningGoalRepository(self._session).get(goal_id)
        tasks = PlanTaskRepository(self._session).list_by_plan(plan.id)
        return L4PlanView(
            goal_id=goal_id,
            goal_description=goal.description if goal else "",
            plan_id=plan.id,
            version=plan.version,
            rationale=str((plan.content or {}).get("rationale", "")),
            tasks=[
                {
                    "task_id": t.id, "week_index": t.week_index,
                    "subject": t.subject, "status": t.status,
                    "related_item_ids": list(t.related_item_ids or []),
                }
                for t in tasks
            ],
            progress=PlanTaskRepository(self._session).progress(plan.id),
        )

    def _latest_view(self, goal_id: str) -> L4PlanView | None:
        plan = LearningPlanRepository(self._session).latest_for_goal(goal_id)
        if plan is None:
            return None
        return self._view(goal_id, plan)


def recent_events_drop(previous: int, current: int) -> bool:
    """活动量下降判定：上一窗口有明显活动，本窗口腰斩以上。"""
    return previous >= 2 and current <= previous / 2