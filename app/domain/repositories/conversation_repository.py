"""会话仓储：创建/读取/追加消息/迁移状态，并强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import select
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
    """派生会话归类：`"l2"`（检测记录）/ `"l1"`（挖掘记录）/ `"chat"`（对话）。

    读时从消息形状派生，不加列不迁移——卡片与 `source` 已经把三种会话说清楚了：

    - 带**任何**冲突检测卡的会话归 `"l2"`（结果卡 `l2_conflicts`，或 pending/failed
      且 `capability="l2"`）——先聊后扫的混合会话也一样：它带着检测记录，就整条
      不进对话历史。
    - 全部消息 `source="l1"` 的会话归 `"l1"`——那是 mine.html 的工具会话（没有
      开场白、没有工作台回合）。工作台里顺手挖过料的对话有开场白和 agent 回合，
      仍是对话。
    - 其余归 `"chat"`。
    """
    for message in messages or []:
        card = message.get("card") or {}
        kind = str(card.get("kind") or "")
        if kind == "l2_conflicts":
            return "l2"
        if kind in ("pending", "failed") and str(card.get("capability") or "") == "l2":
            return "l2"
    if messages and all(str(m.get("source") or "") == "l1" for m in messages):
        return "l1"
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

    def list_by_user(self, user_id: str, kind: str | None = None) -> list[Conversation]:
        """用户全部会话（最近活跃在前），可按派生归类过滤（`kind=None` 不过滤）。

        归类要读消息 JSON 里的卡片与 `source`，SQL 过滤不了——单人规模整表取回
        内存过滤即可。`updated_at` 只精确到秒，同秒时用 id 二级排序保证顺序确定。
        分页请在过滤后的列表上切片（`rows[offset:offset + limit]`）：过滤和
        limit/offset 都交给 SQL 的话，页码和 total 会当场错位。
        """
        self._guard(user_id)
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.asc())
        )
        rows = list(self._session.execute(stmt).scalars().all())
        if kind is None:
            return rows
        return [conv for conv in rows if derive_kind(conv.messages) == kind]

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