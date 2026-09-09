"""最小对话 orchestrator：会话 → 网关 → 回写。

P0 只实现最小聊天流（不接 L1~L5）。L2 冲突、大模型纵深留在后续 Workflow。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models.conversation import Conversation
from app.domain.repositories.conversation_repository import ConversationRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway


class Orchestrator:
    def __init__(self, gateway: ModelGateway, session: Session):
        self._gateway = gateway
        self._session = session

    def chat(self, *, user_id: str, conversation_id: str | None, message: str) -> tuple[Conversation, Completion]:
        """执行一轮对话：取/建会话 → 追加用户输入 → 调网关 → 追加助手回复。

        返回 (会话, 完成结果)。会话需由调用方统一 commit 事务。
        """
        repo = ConversationRepository(self._session, user_id=user_id)
        conversation = repo.get(conversation_id) if conversation_id else None
        if conversation is None:
            conversation = repo.create(user_id=user_id, state="idle")

        repo.append_message(conversation, "user", message)
        messages = conversation.messages or []

        completion = self._gateway.chat(
            task_type="multi_turn_dialogue",
            messages=messages,
            user_id=user_id,
            session=self._session,
        )
        repo.append_message(conversation, "assistant", completion.text)
        return conversation, completion