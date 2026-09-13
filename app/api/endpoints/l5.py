"""L5 归因诊断端点（系统设计 §5.5 / UC-L5-01、UC-L5-02）。

- `POST /l5/diagnosis`：生成归因诊断（本地行为指标 + causal_reasoning 归因 + 置信度校准）
- `GET  /l5/diagnosis/latest`：最新诊断
- `POST /l5/diagnosis/{id}/decision`：采纳 / 拒绝（采纳回写计划参考，拒绝记偏好）
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.l5_orchestrator import L5Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l5", tags=["l5"])


class MetricsOut(BaseModel):
    total_items: int = 0
    completed_items: int = 0
    completion_ratio: float = 0.0
    study_events_week: int = 0
    idle_days: int | None = None
    unseen_conflicts: int = 0


class DiagnosisResponse(BaseModel):
    state: str  # ok | empty | degraded
    diagnosis_id: str = ""
    pattern: str = ""
    root_cause: str = ""
    confidence: float = 0.0
    suggested_action: str = ""
    reasoning_chain: list[str] = []
    metrics: MetricsOut | None = None
    note: str = ""
    request_id: str


class DecisionRequest(BaseModel):
    accepted: bool


class DecisionResponse(BaseModel):
    accepted: bool
    message: str
    request_id: str


def _metrics_out(metrics) -> MetricsOut | None:
    if metrics is None:
        return None
    return MetricsOut(
        total_items=metrics.total_items,
        completed_items=metrics.completed_items,
        completion_ratio=metrics.completion_ratio,
        study_events_week=metrics.study_events_week,
        idle_days=metrics.idle_days,
        unseen_conflicts=metrics.unseen_conflicts,
    )


@router.post("/diagnosis", response_model=DiagnosisResponse)
def diagnose(
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> DiagnosisResponse:
    result = L5Orchestrator(gateway, session).diagnose(user_id=user_id)
    return DiagnosisResponse(
        state=result.state,
        diagnosis_id=result.diagnosis_id,
        pattern=result.pattern,
        root_cause=result.root_cause,
        confidence=result.confidence,
        suggested_action=result.suggested_action,
        reasoning_chain=result.reasoning_chain,
        metrics=_metrics_out(result.metrics),
        note=result.note,
        request_id=trace.get_request_id() or "",
    )


@router.get("/diagnosis/latest", response_model=DiagnosisResponse)
def latest(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> DiagnosisResponse:
    diagnosis = L5Orchestrator(session=session).latest(user_id=user_id)
    if diagnosis is None:
        return DiagnosisResponse(
            state="empty", note="还没有诊断报告，先发起一次归因诊断。",
            request_id=trace.get_request_id() or "",
        )
    return DiagnosisResponse(
        state="ok",
        diagnosis_id=diagnosis.id,
        pattern=diagnosis.pattern,
        root_cause=diagnosis.root_cause,
        confidence=diagnosis.confidence,
        suggested_action=diagnosis.suggested_action,
        reasoning_chain=list(diagnosis.reasoning_chain or []),
        note="",
        request_id=trace.get_request_id() or "",
    )


@router.post("/diagnosis/{diagnosis_id}/decision", response_model=DecisionResponse)
def decide(
    diagnosis_id: str,
    body: DecisionRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> DecisionResponse:
    ok, message = L5Orchestrator(session=session).decide(
        user_id=user_id, diagnosis_id=diagnosis_id, accepted=body.accepted
    )
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return DecisionResponse(
        accepted=body.accepted, message=message, request_id=trace.get_request_id() or ""
    )
