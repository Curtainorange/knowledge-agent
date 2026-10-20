"""统一对话入口端点（对话即入口）。

与 `POST /api/v1/chat` 的区别：chat 是裸对话，这里是**带能力分发的对话**——
用户说什么都往这里发，由 `Copilot` 决定交给哪个能力。原 /chat 保留不动，
旧调用方与既有测试不受影响。

八个端点各管一件事：

- `POST /start`        开启 / 恢复会话——**新会话由副驾主动开场**（进工作台只调它）
- `POST /chat`         一轮对话（可能同步返回结果，也可能返回一张 pending 卡）
- `GET  /conversation` 读会话（前端轮询 pending 卡 + 刷新页面恢复历史）
- `GET  /conversations` 历史会话列表（侧栏；title 为空时现场派生兜底；**默认只回
                      对话**——检测/挖掘记录不进对话历史，kind 参数可取其它归类）
- `PATCH /conversations/{id}`  重命名会话
- `DELETE /conversations/{id}` 删除会话（硬删，含消息记录）
- `POST /actions`      卡片内操作（采纳/忽略）——**不写新消息**，只更新卡片状态
- `GET  /capabilities` 能力清单（「是否已接入」由服务端给，供外部集成与测试用；
                      前端**不再**据此渲染快捷入口按钮——理由见 `web/assets/agent.js`
                      的 `startConversation()`，与 `dev_logs/设计决策与经验.md` 第十八节）
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import turns
from app.agent.copilot import Copilot
from app.agent.router import CAPABILITY_CATALOG, WIRED_CAPABILITIES
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.domain.models.conversation import Conversation
from app.domain.repositories.conversation_repository import (
    ConversationRepository,
    derive_kind,
    derive_title,
)
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


class ConversationBrief(BaseModel):
    """历史列表行：侧栏只展示摘要，消息正文仍走 `GET /conversation/{id}`。"""

    conversation_id: str
    title: str
    state: str
    kind: str
    message_count: int
    preview: str
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ConversationBrief]
    total: int
    limit: int
    offset: int
    request_id: str


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=128)


class ConversationRenameResponse(BaseModel):
    conversation_id: str
    title: str
    request_id: str


class ConversationDeleteResponse(BaseModel):
    conversation_id: str
    deleted: bool
    request_id: str


class AgentActionRequest(BaseModel):
    conversation_id: str
    card_key: str = Field(min_length=1, description="卡片的 key（操作回流时就地替换用）")
    action: str = Field(min_length=1)
    # 不强制非空：有些操作本来就没有目标对象（如「跳过这轮」）。
    # 原来写 min_length=1，结果前端传空串直接 422，跳过按钮点了没反应。
    target_id: str = ""
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


def _first_user_content(conv: Conversation) -> str:
    for message in conv.messages or []:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    messages = conv.messages or []
    return str(messages[0].get("content") or "") if messages else ""


@router.get("/conversations", response_model=ConversationListResponse)
def list_conversations(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    kind: Literal["chat", "l1", "l2", "all"] = Query(
        default="chat",
        description="归类过滤：chat=对话（默认）/ l1=挖掘记录 / l2=检测记录 / all=不过滤",
    ),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConversationListResponse:
    """历史会话列表（最近活跃在前），供侧栏找回旧对话——**默认只含对话**。

    「彻底分开」：检测记录（带冲突卡的会话）与挖掘记录（mine.html 的纯 L1 会话）
    不进对话历史。侧栏不带 kind 参数拿到的就是干净的对话；`kind="l1"`/`"l2"` 留给
    挖掘页 / 检测页各自的记录清单，`kind="all"` 全量（调试与测试）。归类读时从
    消息派生（`derive_kind`），分页在过滤**之后**切，total 与 items 同口径。

    `title` 在库里可能是空串（自动命名上线前的老会话），列表现场派生兜底：
    首条用户消息 → 「新会话」。不回写库——兜底只是展示，回写会让一次列表请求
    意外变成写操作。
    """
    repo = ConversationRepository(session, user_id=user_id)
    rows = repo.list_by_user(user_id, kind=None if kind == "all" else kind)
    total = len(rows)
    items = []
    for conv in rows[offset : offset + limit]:
        first = _first_user_content(conv)
        title = (conv.title or "").strip() or derive_title(first) or "新会话"
        items.append(
            ConversationBrief(
                conversation_id=conv.id,
                title=title,
                state=conv.state,
                kind=derive_kind(conv.messages),
                message_count=len(conv.messages or []),
                preview=derive_title(first, limit=60),
                created_at=conv.created_at,
                updated_at=conv.updated_at,
            )
        )
    return ConversationListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        request_id=trace.get_request_id() or "",
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationRenameResponse)
def rename_conversation(
    conversation_id: str,
    body: ConversationRenameRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConversationRenameResponse:
    """重命名会话。用户改过的名字优先级最高，不会被后续消息的自动命名覆盖。"""
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="标题不能为空")
    repo = ConversationRepository(session, user_id=user_id)
    try:
        conv = repo.rename(conversation_id, title)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该会话") from exc
    if conv is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.commit()
    return ConversationRenameResponse(
        conversation_id=conversation_id,
        title=conv.title,
        request_id=trace.get_request_id() or "",
    )


@router.delete("/conversations/{conversation_id}", response_model=ConversationDeleteResponse)
def delete_conversation(
    conversation_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConversationDeleteResponse:
    """删除会话（硬删整行，消息记录随之消失，不可恢复）。"""
    repo = ConversationRepository(session, user_id=user_id)
    try:
        deleted = repo.delete(conversation_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该会话") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.commit()
    return ConversationDeleteResponse(
        conversation_id=conversation_id,
        deleted=True,
        request_id=trace.get_request_id() or "",
    )


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
