"""L1 认知挖掘端点（D3）：发起/承接多轮澄清挖掘。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.l1_orchestrator import L1Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l1", tags=["l1"])


class L1MineRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class LocatedItemOut(BaseModel):
    """判定命中的条目：带摘要与阅读进度，前端无需再发一次详情请求。"""

    item_id: str
    title: str
    read_progress: float
    snippet: str = ""
    embed_status: str = ""


class CandidateOut(BaseModel):
    """召回候选明细。

    **包含**已命中项——它就是本次召回的完整视图，调用方展示「其他可能」时
    自行排除 `located_items` 中的 id。保留完整列表是为了让调用方能看到
    命中项之外还有什么，以及各自的检索得分与命中通道。
    """

    item_id: str
    title: str
    snippet: str
    score: float
    channels: list[str] = Field(default_factory=list)
    read_progress: float = 0.0


class L1MineResponse(BaseModel):
    state: str  # located | clarifying | empty
    conversation_id: str
    question: str = ""
    located_items: list[LocatedItemOut] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)
    read_hint: str = ""
    turn: int = 0
    max_turns: int = 3
    reason: str = ""
    request_id: str


@router.post("/mine", response_model=L1MineResponse)
def l1_mine(
    body: L1MineRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> L1MineResponse:
    orchestrator = L1Orchestrator(gateway, session)
    result = orchestrator.mine(user_id=user_id, conversation_id=body.conversation_id, message=body.message)
    return L1MineResponse(
        state=result.state,
        conversation_id=result.conversation_id,
        question=result.question,
        located_items=[
            LocatedItemOut(
                item_id=item.item_id,
                title=item.title,
                read_progress=item.read_progress,
                snippet=item.snippet,
                embed_status=item.embed_status,
            )
            for item in result.located_items
        ],
        candidates=[
            CandidateOut(
                item_id=candidate.item_id,
                title=candidate.title,
                snippet=candidate.snippet,
                score=candidate.score,
                channels=candidate.channels,
                read_progress=candidate.read_progress,
            )
            for candidate in result.candidates
        ],
        read_hint=result.read_hint,
        turn=result.turn,
        max_turns=result.max_turns,
        reason=result.reason,
        request_id=trace.get_request_id() or "",
    )