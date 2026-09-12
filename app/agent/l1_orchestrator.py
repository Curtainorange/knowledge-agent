"""L1 认知挖掘编排（D3）：让「多轮追问」有状态。

闭环：双通道召回候选 → 网关结构化输出 L1Route（located|clarify）→
- located：交付命中条目 + 阅读历史提醒
- clarify：生成 1 个澄清问题，续到会话（state=clarifying）
回合上限 max_turns，超限则给最近命中/首候选兜底；知识库为空给明确提示。

状态机复用 Conversation.state（idle/clarifying/located）+ messages[] 承载问答历史。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.gateway import ModelGateway
from app.llm.structure import JsonParseError, parse_structured
from app.retrieval.embedding import build_embedding
from app.retrieval.retriever import RetrievedItem, Retriever

logger = logging.getLogger(__name__)


class L1Route(BaseModel):
    """LLM 结构化路由决策（ADR-11 schema）。item_ids 仅在 located 时给出。"""
    decision: Literal["located", "clarify"]
    item_ids: list[str] = Field(default_factory=list)
    question: str = Field(default="", description="clarify 时向用户提出的问题")
    reason: str = Field(default="", description="决策依据（可空）")


@dataclass
class LocatedItem:
    item_id: str
    title: str
    read_progress: float
    # 定位后直接给出摘要与向量化状态，调用方无需二次请求即可展示
    snippet: str = ""
    embed_status: str = ""


@dataclass
class CandidateItem:
    """召回候选明细。

    即使某条没被判定为命中，也让用户看到「还有哪些可能」——只给一条结果时，
    用户无从判断模型是真定位到了，还是随便挑了一条。
    """

    item_id: str
    title: str
    snippet: str
    score: float
    channels: list[str] = field(default_factory=list)
    read_progress: float = 0.0


@dataclass
class L1Result:
    state: Literal["located", "clarifying", "empty"]
    conversation_id: str = ""
    question: str = ""
    located_items: list[LocatedItem] = field(default_factory=list)
    candidates: list[CandidateItem] = field(default_factory=list)
    read_hint: str = ""
    turn: int = 0          # 已进行的追问轮次
    max_turns: int = 3     # 追问上限
    reason: str = ""       # 模型给出的决策依据（便于排查与展示）


_SYS = (
    "你是「认知副驾」的 L1 认知挖掘器。用户给出一条模糊线索，你要从候选知识条目里"
    "判断能否精准定位。可定位（证据充分、线索唯一对应某条）→ located，给出 item_ids；"
    "否则 → clarify，提出 1 个最有效、不多问的澄清问题帮助收敛。输出严格 JSON："
    '{"decision":"located|clarify","item_ids":[],"question":"","reason":""}。'
)


def _read_hint(item: KnowledgeItem) -> str:
    """按真实阅读进度给出提醒；已读完则不提示。

    read_progress 由 PATCH /api/v1/knowledge/items/{id} 写入，因此该提醒会随
    用户行为变化——早期它是恒真的死信号（字段无处更新），现在已可写。
    """
    progress = item.read_progress or 0.0
    if progress >= 1.0:
        return ""
    if progress <= 0.0:
        return "这条你还没开始读，建议先读一遍再复用"
    return f"这条已读 {int(round(progress * 100))}%，还没读完，建议补完再复用"


class L1Orchestrator:
    def __init__(
        self,
        gateway: ModelGateway,
        session: Session,
        retriever: Retriever | None = None,
        max_turns: int = 3,
    ) -> None:
        self._gateway = gateway
        self._session = session
        self._retriever = retriever or Retriever(build_embedding())
        self._max_turns = max_turns

    # ---- 内部工具 ------------------------------------------------------

    def _load_conversation(self, user_id: str, conversation_id: str | None):
        repo = ConversationRepository(self._session, user_id=user_id)
        if conversation_id:
            conv = repo.get(conversation_id)
            if conv is not None:
                return conv
        return repo.create(user_id)

    @staticmethod
    def _l1_turn_count(conv) -> int:
        """已进行的 L1 追问轮次。

        只统计带 `source="l1"` 标记的 assistant 消息：同一会话若混用过通用对话，
        那些回复不该计入 L1 的追问上限（旧实现数了所有 assistant 消息，会提前触发兜底）。
        """
        return sum(
            1 for m in (conv.messages or [])
            if m.get("role") == "assistant" and m.get("source") == "l1"
        )

    @staticmethod
    def _candidate_details(
        candidates: list[RetrievedItem], items: dict[str, KnowledgeItem]
    ) -> list[CandidateItem]:
        """把检索结果补全为可直接展示的候选明细（检索层只给 id/分数，不含文本）。"""
        details: list[CandidateItem] = []
        for candidate in candidates:
            item = items.get(candidate.item_id)
            if item is None:
                continue
            details.append(
                CandidateItem(
                    item_id=item.id,
                    title=item.title,
                    snippet=item.snippet,
                    score=round(candidate.score, 4),
                    channels=list(candidate.channels),
                    read_progress=item.read_progress or 0.0,
                )
            )
        return details

    @staticmethod
    def _candidate_block(candidates: list[RetrievedItem], items: dict[str, KnowledgeItem]) -> str:
        lines = []
        for i, c in enumerate(candidates, 1):
            it = items.get(c.item_id)
            if it is None:
                continue
            snippet = (it.raw_content or "")[:120].replace("\n", " ")
            lines.append(f"[{i}] id={it.id} title={it.title} 摘要={snippet}")
        return "\n".join(lines) or "（无候选条目）"

    def _prompt(self, conv, message: str, candidates, items) -> list[dict]:
        # 会话历史去掉刚追加的本次 user 消息，避免重复带入
        history = [{"role": m.get("role"), "content": m.get("content", "")} for m in (conv.messages or [])[:-1]]
        context = "以下候选条目来自用户知识库（含 id、标题、摘要）：\n" + self._candidate_block(candidates, items)
        msg = f"{context}\n\n用户的线索/上下文：\n{message}\n\n请输出 L1Route JSON。"
        return [{"role": "system", "content": _SYS}, *history, {"role": "user", "content": msg}]

    def _route(self, conv, message, candidates, items, user_id: str) -> L1Route:
        messages = self._prompt(conv, message, candidates, items)
        completion = self._gateway.chat(
            task_type="l1_mining", messages=messages, user_id=user_id, session=self._session
        )
        try:
            return parse_structured(completion.text, validator=lambda d: L1Route(**d))
        except JsonParseError as exc:
            logger.warning("l1 parse failed fallback clarify: %s", exc)
            return L1Route(decision="clarify", question="你能再描述得具体一点吗（时间、对象或场景）？")

    # ---- 对外入口 ------------------------------------------------------

    def mine(self, *, user_id: str, conversation_id: str | None, message: str) -> L1Result:
        conv = self._load_conversation(user_id, conversation_id)
        cid = conv.id
        repo = ConversationRepository(self._session, user_id=user_id)
        repo.append_message(conv, "user", message, source="l1")
        self._session.flush()

        krepo = KnowledgeRepository(self._session, user_id=user_id)
        items = {i.id: i for i in krepo.list_active(user_id)}
        if not items:
            repo.set_state(conv, "idle")
            self._session.commit()
            return L1Result(
                state="empty",
                conversation_id=cid,
                question="知识库还是空的，先录入几条知识再来挖掘吧。",
                max_turns=self._max_turns,
            )

        turn = self._l1_turn_count(conv)
        candidates = self._retriever.retrieve(message, list(items.values()), top_k=5)
        details = self._candidate_details(candidates, items)

        # 追问超限：兜底定位最可能的候选，结束追问（宁可给一条，也不要无限问下去）
        if turn >= self._max_turns and candidates:
            return self._finish_located(
                conv, repo, [candidates[0].item_id], items,
                candidates=details, turn=turn, reason="追问已达上限，回退到最可能的候选",
            )

        route = self._route(conv, message, candidates, items, user_id)

        if route.decision == "located":
            valid = [item_id for item_id in route.item_ids if item_id in items]
            if valid:
                return self._finish_located(
                    conv, repo, valid, items,
                    candidates=details, turn=turn, reason=route.reason,
                )
            logger.warning("l1 路由给出不存在的 item_ids，降级为追问：%s", route.item_ids)

        # clarify：记一条带来源标记的追问，续聊下一轮
        question = route.question or "线索还不够明确，请再多给一点上下文？"
        repo.append_message(conv, "assistant", question, source="l1")
        repo.set_state(conv, "clarifying")
        self._session.commit()
        return L1Result(
            state="clarifying",
            conversation_id=cid,
            question=question,
            candidates=details,
            turn=turn + 1,
            max_turns=self._max_turns,
            reason=route.reason,
        )

    def _finish_located(
        self,
        conv,
        repo,
        item_ids: list[str],
        items: dict[str, KnowledgeItem],
        *,
        candidates: list[CandidateItem] | None = None,
        turn: int = 0,
        reason: str = "",
        max_items: int = 3,
    ) -> L1Result:
        """交付命中条目（支持多条）。

        去重、丢弃不存在的 id，并截断到 max_items：模型偶尔会返回重复 id 或一长串
        候选，直接全量返回等于把整个知识库倒给用户。
        """
        located: list[LocatedItem] = []
        seen: set[str] = set()
        for item_id in item_ids:
            if item_id in seen or item_id not in items:
                continue
            seen.add(item_id)
            it = items[item_id]
            located.append(
                LocatedItem(
                    item_id=it.id,
                    title=it.title,
                    read_progress=it.read_progress,
                    snippet=it.snippet,
                    embed_status=it.embed_status,
                )
            )
            if len(located) >= max_items:
                break

        hint = _read_hint(items[located[0].item_id]) if located else ""
        repo.set_state(conv, "located")
        self._session.commit()
        logger.info(
            "l1 located items=%s hint=%r conv=%s", [l.item_id for l in located], hint, conv.id
        )
        return L1Result(
            state="located",
            conversation_id=conv.id,
            located_items=located,
            candidates=candidates or [],
            read_hint=hint,
            turn=turn,
            max_turns=self._max_turns,
            reason=reason,
        )