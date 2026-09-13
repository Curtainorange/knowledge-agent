"""L3 认知助产端点（系统设计 §5.3 / UC-L3-01、UC-L3-02）。

- `POST /l3/brief`：主题分布 + 认知深度 + 「大量存在/完全缺失」模式 + 2-3 个追问
  + 本周冲突同比（UC-L3-01 的主体；推送通道待 ADR-14）
- `POST /l3/question`：围绕指定条目生成 1 个衔接追问（UC-L3-02）

两条都是**被动调用**：主动推送要等推送与抑制服务落地，否则"生成即调用"
会在每次录入时多花一次模型调用却无处送达。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.l3_orchestrator import L3Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l3", tags=["l3"])


class TopicStatOut(BaseModel):
    topic: str
    count: int
    levels: dict[str, int] = Field(default_factory=dict)


class QuestionOut(BaseModel):
    question: str
    why: str = ""
    evidence: str = ""
    next_step: str = ""


class L3BriefResponse(BaseModel):
    state: str  # ok | empty | degraded
    analyzed_items: int = 0
    topics: list[TopicStatOut] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)
    questions: list[QuestionOut] = Field(default_factory=list)
    conflict_stats: dict = Field(default_factory=dict)
    overview: str = ""
    note: str = ""
    request_id: str


class L3QuestionRequest(BaseModel):
    item_id: str = Field(min_length=1)


def _to_response(result, request_id: str) -> L3BriefResponse:
    return L3BriefResponse(
        state=result.state,
        analyzed_items=result.analyzed_items,
        topics=[
            TopicStatOut(topic=s.topic, count=s.count, levels=s.levels) for s in result.topics
        ],
        patterns=result.patterns,
        questions=[
            QuestionOut(
                question=q.question, why=q.why, evidence=q.evidence, next_step=q.next_step
            )
            for q in result.questions
        ],
        conflict_stats=result.conflict_stats,
        overview=result.overview,
        note=result.note,
        request_id=request_id,
    )


@router.post("/brief", response_model=L3BriefResponse)
def brief(
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> L3BriefResponse:
    """分析知识结构并生成「该问但没问」的追问。"""
    result = L3Orchestrator(gateway, session).brief(user_id=user_id)

    from app.feedback import events

    events.record(
        session, user_id=user_id, event_type=events.L3_BRIEF,
        payload={
            "state": result.state,
            "analyzed_items": result.analyzed_items,
            "topics": len(result.topics),
            "questions": len(result.questions),
            "conflicts_this_week": result.conflict_stats.get("this_week", 0),
        },
    )
    return _to_response(result, trace.get_request_id() or "")


@router.post("/question", response_model=L3BriefResponse)
def question(
    body: L3QuestionRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> L3BriefResponse:
    """围绕指定条目生成 1 个衔接追问（UC-L3-02）。"""
    try:
        result = L3Orchestrator(gateway, session).question_for_item(
            user_id=user_id, item_id=body.item_id
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该条目") from exc

    from app.feedback import events

    events.record(
        session, user_id=user_id, event_type=events.L3_QUESTION,
        payload={"state": result.state, "item_id": body.item_id, "questions": len(result.questions)},
    )
    return _to_response(result, trace.get_request_id() or "")