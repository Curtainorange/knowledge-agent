"""统一对话入口端点 POST /api/v1/agent/chat（对话即入口）。

与 `POST /api/v1/chat` 的区别：chat 是裸对话，这里是**带能力分发的对话**——
用户说什么都往这里发，由 `Copilot` 决定交给哪个能力。原 /chat 保留不动，
旧调用方与既有测试不受影响。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.copilot import Copilot
from app.agent.router import CAPABILITY_CATALOG, WIRED_CAPABILITIES
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class AgentChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = Field(min_length=1, max_length=4000)


class AgentChatResponse(BaseModel):
    conversation_id: str
    capability: str
    reply: str
    card: dict[str, Any] | None = None
    state: str = "idle"
    decided_by: str = ""
    request_id: str


class CapabilityOut(BaseModel):
    """能力目录项：前端据此渲染快捷入口，并把「未接入」的标出来。"""

    capability: str
    label: str
    example: str
    wired: bool
    href: str = ""


@router.post("/chat", response_model=AgentChatResponse)
def agent_chat(
    body: AgentChatRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> AgentChatResponse:
    turn = Copilot(gateway, session).handle(
        user_id=user_id,
        conversation_id=body.conversation_id,
        message=body.message,
    )
    # 事务已由 Copilot 提交（各能力内部还有各自的提交点），这里只负责组织回包
    return AgentChatResponse(**turn.as_dict(), request_id=trace.get_request_id() or "")


@router.get("/capabilities", response_model=list[CapabilityOut])
def agent_capabilities() -> list[CapabilityOut]:
    """对话入口能做的事。

    快捷入口的文案与「是否已接入」都由服务端给：前端硬编码一份必然与路由规则漂移
    （示例话术改了、本地规则没同步，点按钮就变成一次模型调用甚至兜底成闲聊）。
    本接口不需要鉴权——它不含任何用户数据，只是能力清单。
    """
    return [
        CapabilityOut(
            capability=spec.capability,
            label=spec.label,
            example=spec.example,
            wired=spec.capability in WIRED_CAPABILITIES,
            href=spec.href,
        )
        for spec in CAPABILITY_CATALOG
    ]
