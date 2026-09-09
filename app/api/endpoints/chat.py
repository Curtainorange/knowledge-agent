"""对话端点 POST /api/v1/chat（P0 非流式；SSE 见需求 8.4，后续实现）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.orchestrator import Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1", tags=["chat"])


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


class ChatResponse(BaseModel):
    conversation_id: str
    reply: str
    request_id: str


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> ChatResponse:
    orchestrator = Orchestrator(gateway, session)
    conversation, completion = orchestrator.chat(
        user_id=user_id, conversation_id=body.conversation_id, message=body.message,
    )
    session.commit()  # 事务提交：会话消息 + 成本日志同事务持久化
    return ChatResponse(
        conversation_id=conversation.id,
        reply=completion.text,
        request_id=trace.get_request_id() or "",
    )