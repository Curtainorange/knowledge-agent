"""会话仓储：创建/读取/追加消息/迁移状态，并强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models.conversation import Conversation
from app.domain.repositories.base import BaseRepository

_TITLE_LIMIT = 40


def derive_title(content: str, limit: int = _TITLE_LIMIT) -> str:
    """从一段消息文本派生会话标题：换行/制表→空格、压缩空白、超长截断加省略号。

    侧栏是单行布局，换行和连续空白都会撑破它；标题只是列表里的辨认锚点，
    截断到 `limit` 字足够，完整内容仍在消息流里。
    """
    text = " ".join((content or "").split())
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def derive_kind(messages: list[dict] | None) -> str:
    """派生会话归类：`"l2"`（冲突检测）或 `"chat"`（对话）。

    两类必须分开（2026-10-12）：冲突检测跑在对话里时会留下扫描卡，会话接着
    进历史侧栏，「帮我检测冲突」这类一次性运行就和真正的聊天混排在一起。
    侧栏据此分「对话 / 冲突检测」两段——同在一张列表里，切换/继续不受影响，
    只是不再混着看。

    判定只认**卡片协议**（`app/agent/cards.py`，字面量在这里刻意不 import——
    领域层不能反向依赖能力层）：l2 结果卡 `kind="l2_conflicts"`，或还在跑/失败的
    占位卡 `kind` 为 pending/failed 且 `capability="l2"`。先聊后扫的混合会话也算
    `"l2"`——它带着检测记录，归到检测段比混进对话段更诚实。
    其余一切（闲聊、录入、挖掘、同步…）都是 `"chat"`；要不要再细分，等有需要再说。
    """
    for message in messages or []:
        card = message.get("card") or {}
        kind = str(card.get("kind") or "")
        if kind == "l2_conflicts":
            return "l2"
        if kind in ("pending", "failed") and str(card.get("capability") or "") == "l2":
            return "l2"
    return "chat"


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

    def append_message(
        self,
        conversation: Conversation,
        role: str,
        content: str,
        source: str | None = None,
        card: dict | None = None,
    ) -> None:
        """追加一条消息。

        `source` 标记消息来源（如 "l1"）：同一会话可能既走通用对话又走 L1 澄清，
        带上来源才能把「本轮 L1 追问次数」与其它消息区分开，避免相互污染计数。

        `card` 承载结构化结果（命中条目、冲突列表、计划表…）：对话入口用一张卡片
        代替一段散文，前端才能把「采纳 / 忽略 / 查看全文」这类操作摆在结果旁边。
        只挂在**有结果的回合**上；追问、闲聊这类纯文本回合不带卡。

        注意 `messages` 是 JSON 列且此处整体重新赋值——SQLAlchemy 不会追踪 JSON 内部
        的原地修改，只有重新赋值才会落库。

        **自动命名也在这里**：首条用户消息派生 `title`（write-once）。写在这层是因为
        所有写消息的路径（Orchestrator / Copilot / L1 / turns）都经过本方法，写在任何
        一个调用方都会漏掉其它路径。用户重命名后 `title` 不再为空，后续消息不会覆盖——
        重命名是显式意图，不能被冲掉。
        """
        messages = list(conversation.messages or [])
        message: dict = {"role": role, "content": content}
        if source:
            message["source"] = source
        if card:
            message["card"] = card
        messages.append(message)
        conversation.messages = messages
        if role == "user" and not (conversation.title or "").strip():
            conversation.title = derive_title(content)

    def set_state(self, conversation: Conversation, state: str) -> None:
        conversation.state = state

    def count_by_user(self, user_id: str) -> int:
        self._guard(user_id)
        stmt = select(func.count()).select_from(Conversation).where(Conversation.user_id == user_id)
        return int(self._session.execute(stmt).scalar_one())

    def page_by_user(self, user_id: str, limit: int = 50, offset: int = 0) -> list[Conversation]:
        """按最近活跃分页（`updated_at` 只精确到秒，用 id 二级排序保证顺序确定）。"""
        self._guard(user_id)
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.asc())
            .limit(limit)
            .offset(offset)
        )
        return list(self._session.execute(stmt).scalars().all())

    def rename(self, conversation_id: str, title: str) -> Conversation | None:
        conv = self.get(conversation_id)
        if conv is None:
            return None
        conv.title = title
        return conv

    def delete(self, conversation_id: str) -> bool:
        conv = self.get(conversation_id)
        if conv is None:
            return False
        self._session.delete(conv)
        self._session.flush()
        return True