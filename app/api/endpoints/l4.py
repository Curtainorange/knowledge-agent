"""L4 路径修正端点（系统设计 §5.4 / UC-L4-01、UC-L4-02）。

- `POST /l4/goals`：设定学习目标（UC-L4-01 第 1 步）
- `GET  /l4/goals`：目标列表
- `POST /l4/goals/{id}/plan`：拆解为周维度计划（可反复调用以重排；版本号递增）
- `GET  /l4/plan`：当前计划 + 任务 + 完成度
- `POST /l4/deviation/check`：本地统计 + 必要时归因，返回偏离信号与调整建议
  （**只提议不落地**，等用户决定；本地无偏离时不会调用模型）
- `POST /l4/deviation/{plan_id}/decision`：用户是否同意调整；同意才重拆计划
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.l4_orchestrator import L4Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l4", tags=["l4"])


class GoalCreateRequest(BaseModel):
    description: str = Field(min_length=2, max_length=512)
    deadline: datetime | None = None
    priority: str = Field(default="medium", pattern="^(high|medium|low)$")


class GoalOut(BaseModel):
    goal_id: str
    description: str
    priority: str
    deadline: datetime | None = None
    achieved: bool = False


class GoalListResponse(BaseModel):
    items: list[GoalOut]
    total: int
    request_id: str


class GoalCreateResponse(BaseModel):
    goal_id: str
    request_id: str


class PlanTaskOut(BaseModel):
    task_id: str
    week_index: int
    subject: str
    status: str
    related_item_ids: list[str] = Field(default_factory=list)


class PlanResponse(BaseModel):
    state: str = "ok"  # ok | degraded（目标在、但模型这次没给出可用计划）
    goal_id: str = ""
    goal_description: str = ""
    plan_id: str = ""
    version: int = 0
    rationale: str = ""
    tasks: list[PlanTaskOut] = Field(default_factory=list)
    progress: dict = Field(default_factory=dict)
    note: str = ""
    request_id: str


class SignalsOut(BaseModel):
    window_days: int
    idle_days: int | None = None
    recent_events: int = 0
    previous_events: int = 0
    recent_new_items: int = 0
    previous_new_items: int = 0
    plan_total: int = 0
    plan_done: int = 0
    topic_overlap: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class DeviationResponse(BaseModel):
    state: str  # ok | no_plan | no_deviation | degraded
    plan_id: str = ""
    signals: SignalsOut | None = None
    root_cause: str = ""
    adjustment: str = ""
    expected_gain: str = ""
    confidence: float = 0.0
    note: str = ""
    request_id: str


class DecisionRequest(BaseModel):
    accepted: bool


class DecisionResponse(BaseModel):
    accepted: bool
    message: str
    plan: PlanResponse | None = None
    request_id: str


def _plan_response(view, request_id: str, note: str = "", state: str = "ok") -> PlanResponse:
    if view is None:
        return PlanResponse(state=state, note=note, request_id=request_id)
    return PlanResponse(
        state=state,
        goal_id=view.goal_id,
        goal_description=view.goal_description,
        plan_id=view.plan_id,
        version=view.version,
        rationale=view.rationale,
        tasks=[PlanTaskOut(**t) for t in view.tasks],
        progress=view.progress,
        note=note,
        request_id=request_id,
    )


@router.post("/goals", response_model=GoalCreateResponse, status_code=201)
def create_goal(
    body: GoalCreateRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> GoalCreateResponse:
    goal_id = L4Orchestrator(session=session).create_goal(  # 建目标不需要模型
        user_id=user_id, description=body.description, deadline=body.deadline, priority=body.priority
    )
    return GoalCreateResponse(goal_id=goal_id, request_id=trace.get_request_id() or "")


@router.get("/goals", response_model=GoalListResponse)
def list_goals(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> GoalListResponse:
    goals = L4Orchestrator(session=session).list_goals(user_id=user_id)
    return GoalListResponse(
        items=[GoalOut(**g) for g in goals], total=len(goals),
        request_id=trace.get_request_id() or "",
    )


@router.post("/goals/{goal_id}/plan", response_model=PlanResponse)
def generate_plan(
    goal_id: str,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> PlanResponse:
    result = L4Orchestrator(gateway, session).generate_plan(user_id=user_id, goal_id=goal_id)
    if result is None:
        raise HTTPException(status_code=404, detail="目标不存在")
    return _plan_response(result.view, trace.get_request_id() or "", result.note, result.state)


@router.get("/plan", response_model=PlanResponse)
def latest_plan(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> PlanResponse:
    view = L4Orchestrator(session=session).latest_plan_view(user_id=user_id)
    if view is None:
        return PlanResponse(
            state="empty", note="还没有计划，先设定目标。", request_id=trace.get_request_id() or ""
        )
    return _plan_response(view, trace.get_request_id() or "")


@router.post("/deviation/check", response_model=DeviationResponse)
def check_deviation(
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> DeviationResponse:
    report = L4Orchestrator(gateway, session).check_deviation(user_id=user_id)
    signals = (
        SignalsOut(
            window_days=report.signals.window_days,
            idle_days=report.signals.idle_days,
            recent_events=report.signals.recent_events,
            previous_events=report.signals.previous_events,
            recent_new_items=report.signals.recent_new_items,
            previous_new_items=report.signals.previous_new_items,
            plan_total=report.signals.plan_total,
            plan_done=report.signals.plan_done,
            topic_overlap=report.signals.topic_overlap,
            reasons=report.signals.reasons,
        )
        if report.signals
        else None
    )
    return DeviationResponse(
        state=report.state,
        plan_id=report.plan_id,
        signals=signals,
        root_cause=report.analysis.root_cause if report.analysis else "",
        adjustment=report.analysis.adjustment if report.analysis else "",
        expected_gain=report.analysis.expected_gain if report.analysis else "",
        confidence=report.analysis.confidence if report.analysis else 0.0,
        note=report.note,
        request_id=trace.get_request_id() or "",
    )


@router.post("/deviation/{plan_id}/decision", response_model=DecisionResponse)
def decide(
    plan_id: str,
    body: DecisionRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> DecisionResponse:
    try:
        ok, message = L4Orchestrator(gateway, session).decide_adjustment(
            user_id=user_id, plan_id=plan_id, accepted=body.accepted
        )
    except PermissionError as exc:
        # 仓储的结构性越权防护：别人的计划一律 403，不能泄漏"这个 id 存在"
        raise HTTPException(status_code=403, detail="无权访问该计划") from exc
    if not ok:
        raise HTTPException(status_code=404, detail=message)

    view = L4Orchestrator(session=session).latest_plan_view(user_id=user_id)
    return DecisionResponse(
        accepted=body.accepted,
        message=message,
        plan=_plan_response(view, trace.get_request_id() or "") if view else None,
        request_id=trace.get_request_id() or "",
    )