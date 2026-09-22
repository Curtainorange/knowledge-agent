"""最小对话 orchestrator：会话 → 检索知识上下文 → 网关 → 回写。

P0 的聊天流（不接 L1~L5 的卡片式能力，那些走对话入口 copilot）。
这里负责让「闲聊 / 提问」也带上认知副驾的人设与用户已存的知识上下文，
而不是一个对自己知识库一无所知的裸 LLM。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.domain.models.conversation import Conversation
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.prompts import COPILOT_PERSONA
from app.retrieval.embedding import build_embedding
from app.retrieval.retriever import Retriever

logger = logging.getLogger(__name__)

# 闲聊时注入知识库上下文的最多条目数：够让模型「懂你」，又不至于把整库倒进上下文
_CHAT_CONTEXT_TOP_K = 3
# 每条注入上下文的正文截断长度（字符）
_CHAT_CONTEXT_SNIPPET = 160


class Orchestrator:
    def __init__(
        self,
        gateway: ModelGateway,
        session: Session,
        retriever: Retriever | None = None,
    ) -> None:
        self._gateway = gateway
        self._session = session
        self._retriever = retriever  # 惰性构建，测试可注入假件

    def chat(self, *, user_id: str, conversation_id: str | None, message: str) -> tuple[Conversation, Completion]:
        """执行一轮对话：取/建会话 → 追加用户输入 → 注入人设与知识上下文 → 调网关 → 追加助手回复。

        返回 (会话, 完成结果)。会话需由调用方统一 commit 事务。
        """
        repo = ConversationRepository(self._session, user_id=user_id)
        conversation = repo.get(conversation_id) if conversation_id else None
        if conversation is None:
            conversation = repo.create(user_id=user_id, state="idle")

        repo.append_message(conversation, "user", message)
        history = conversation.messages or []

        messages = self._build_messages(user_id, message, history)
        completion = self._gateway.chat(
            task_type="multi_turn_dialogue",
            messages=messages,
            user_id=user_id,
            session=self._session,
        )
        repo.append_message(conversation, "assistant", completion.text)
        return conversation, completion

    # ---- 内部 ------------------------------------------------------------

    @staticmethod
    def _clean_history(history: list[dict]) -> list[dict]:
        """只保留 role/content 给模型。

        会话历史里可能混着能力卡片（card）、来源标记（source）等展示字段，
        把它们原样发给模型既是噪音、又可能让 OpenAI 兼容协议报未知字段——
        这里收敛成模型真正需要的两种字段。
        """
        return [
            {"role": m.get("role", "user"), "content": m.get("content", "") or ""}
            for m in (history or [])
        ]

    def _knowledge_context(self, user_id: str, message: str) -> str:
        """检索用户知识库中与当前话题相关的条目，拼成一段简短上下文。

        任何异常（无条目 / 向量不可用 / 检索抛错）都静默降级为空串：
        闲聊不该因为检索挂掉而整轮失败。
        """
        try:
            krepo = KnowledgeRepository(self._session, user_id=user_id)
            items = {i.id: i for i in krepo.list_active(user_id)}
            if not items:
                return ""
            retriever = self._retriever or Retriever(build_embedding())
            top = retriever.retrieve(message, user_id=user_id, top_k=_CHAT_CONTEXT_TOP_K)
        except Exception as exc:  # pragma: no cover - 防御性兜底
            logger.warning("chat knowledge context skipped: %s", exc)
            return ""

        lines: list[str] = []
        for hit in top:
            item = items.get(hit.item_id)
            if item is None:
                continue
            snippet = (item.raw_content or "").replace("\n", " ").strip()[:_CHAT_CONTEXT_SNIPPET]
            lines.append(f"- {item.title}：{snippet}")
        if not lines:
            return ""
        return (
            "以下是你知识库里与当前话题可能相关的内容，供参考（不要逐字复述，"
            "除非用户问起）：\n" + "\n".join(lines)
        )

    def _build_messages(self, user_id: str, message: str, history: list[dict]) -> list[dict]:
        """组装发往模型的完整消息：人设 + 知识上下文 + 干净历史。"""
        head: list[dict] = [{"role": "system", "content": COPILOT_PERSONA.text}]
        context = self._knowledge_context(user_id, message)
        if context:
            head.append({"role": "system", "content": context})
        return [*head, *self._clean_history(history)]
