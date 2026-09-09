"""L1 认知挖掘端点（D3）：发起/承接多轮澄清挖掘。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.l1_orchestrator import L1Orchestrator, LocatedItem
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l1", tags=["l1"])


class L1MineRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = None


class L1MineResponse(BaseModel):
    state: str  # located | clarifying | empty
    conversation_id: str
    question: str = ""
    located_items: list[LocatedItem] = Field(default_factory=list)
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
        located_items=result.located_items,
        request_id=trace.get_request_id() or "",
    )