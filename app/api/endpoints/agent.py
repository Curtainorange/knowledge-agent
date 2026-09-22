"""统一对话入口端点（对话即入口）。

与 `POST /api/v1/chat` 的区别：chat 是裸对话，这里是**带能力分发的对话**——
用户说什么都往这里发，由 `Copilot` 决定交给哪个能力。原 /chat 保留不动，
旧调用方与既有测试不受影响。

五个端点各管一件事：

- `POST /start`        开启 / 恢复会话——**新会话由副驾主动开场**（进工作台只调它）
- `POST /chat`         一轮对话（可能同步返回结果，也可能返回一张 pending 卡）
- `GET  /conversation` 读会话（前端轮询 pending 卡 + 刷新页面恢复历史）
- `POST /actions`      卡片内操作（采纳/忽略）——**不写新消息**，只更新卡片状态
- `GET  /capabilities` 能力清单（「是否已接入」由服务端给，供外部集成与测试用；
                      前端**不再**据此渲染快捷入口按钮——理由见 `web/assets/agent.js`
                      的 `startConversation()`，与 `dev_logs/设计决策与经验.md` 第十八节）
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import turns
from app.agent.copilot import Copilot
from app.agent.router import CAPABILITY_CATALOG, WIRED_CAPABILITIES
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


class AgentStartRequest(BaseModel):
    conversation_id: str | None = None


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


class ConversationMessageOut(BaseModel):
    role: str
    content: str
    source: str = ""
    card: dict[str, Any] | None = None


class ConversationOut(BaseModel):
    conversation_id: str
    state: str
    messages: list[ConversationMessageOut]
    request_id: str


class AgentActionRequest(BaseModel):
    conversation_id: str
    card_key: str = Field(min_length=1, description="卡片的 key（操作回流时就地替换用）")
    action: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    value: str = ""


class AgentActionResponse(BaseModel):
    card_key: str
    card: dict[str, Any]
    reply: str
    request_id: str


class CapabilityOut(BaseModel):
    """能力目录项：前端据此渲染快捷入口，并把「未接入」的标出来。"""

    capability: str
    label: str
    example: str
    wired: bool
    href: str = ""


@router.post("/start", response_model=ConversationOut)
def agent_start(
    body: AgentStartRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> ConversationOut:
    """开启 / 恢复会话：新会话由副驾主动开场。

    与 `GET /conversation` 的分工：那个是纯读（轮询 pending 卡、恢复历史），
    这个会在**新会话**上写一条开场消息，所以是 POST。

    前端进工作台只调这一个就够——「恢复历史」与「新会话开场」本就是同一件事的两面。
    已有消息的会话不会被重新开场：刷新一次页面就多一句问候，是最容易被看穿的假智能。
    """
    conversation_id = Copilot(gateway, session).start(
        user_id=user_id, conversation_id=body.conversation_id
    )
    data = turns.read_conversation(session, user_id=user_id, conversation_id=conversation_id)
    return ConversationOut(**data, request_id=trace.get_request_id() or "")


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


@router.get("/conversation/{conversation_id}", response_model=ConversationOut)
def agent_conversation(
    conversation_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConversationOut:
    """读一个会话的全部消息与卡片。

    前端用它对两件事：轮询 pending 卡是否出结果；刷新页面后恢复历史。
    返回值里的卡片是**按当前数据库刷新过**的——否则已经处理过的冲突
    会重新显示成待处理，卡片就成了假的。
    """
    try:
        data = turns.read_conversation(
            session, user_id=user_id, conversation_id=conversation_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="会话不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该会话") from exc
    return ConversationOut(**data, request_id=trace.get_request_id() or "")


@router.post("/actions", response_model=AgentActionResponse)
def agent_action(
    body: AgentActionRequest,
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> AgentActionResponse:
    """卡片内操作（采纳 / 忽略 / 保持原计划 / 按建议重排）。

    刻意**不追加会话消息**：对已有结果的处置不是新的一轮对话，写一条「已忽略」
    只会把消息流刷满噪声。动作本身由学习事件记录（L2 反馈 / L4 决策 / L5 决策），
    这就是那份留痕；卡片状态则通过刷新保持真实。

    慢操作（按建议重排计划）会返回 pending 卡，由前端轮询等结果；快操作直接返回更新后的卡片。
    """
    try:
        reply, card = turns.apply_action(
            session,
            user_id=user_id,
            conversation_id=body.conversation_id,
            card_key=body.card_key,
            action=body.action,
            target_id=body.target_id,
            value=body.value,
            gateway=gateway,
        )
    except turns.ActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgentActionResponse(
        card_key=body.card_key,
        card=card,
        reply=reply,
        request_id=trace.get_request_id() or "",
    )


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
