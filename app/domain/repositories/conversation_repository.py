"""会话仓储：创建/读取/追加消息/迁移状态，并强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models.conversation import Conversation
from app.domain.repositories.base import BaseRepository


class ConversationRepository(BaseRepository[Conversation]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(self, user_id: str, state: str = "idle") -> Conversation:
        conv = Conversation(user_id=user_id, state=state)
        self._session.add(conv)
        self._session.flush()
        return conv

    def get(self, conversation_id: str) -> Conversation | None:
        conv = self._session.get(Conversation, conversation_id)
        if conv is None:
            return None
        self._guard(conv.user_id)  # 绑定用户时，禁止越权读取他人会话
        return conv

    def append_message(self, conversation: Conversation, role: str, content: str) -> None:
        messages = list(conversation.messages or [])
        messages.append({"role": role, "content": content})
        conversation.messages = messages

    def set_state(self, conversation: Conversation, state: str) -> None:
        conversation.state = state