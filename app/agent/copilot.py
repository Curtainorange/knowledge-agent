"""统一对话编排：一句话进来，出去的是「一句回复 + 一张卡片」。

这是「对话即入口」的落点。它本身**不实现任何能力**，只做三件事：

1. **承接**：上一轮 L1 还在等澄清时，把这句话优先当成对追问的回答（见 router 的说明）。
2. **分发**：按 `CapabilityRoute` 把话交给对应的编排器/服务。
3. **收口**：把结果整理成统一卡片写回会话，前端只认卡片、不认能力细节。

---

**谁负责追加消息？** 这里有一条容易踩的约定：

- 各能力**自己追加自己的消息**。`Orchestrator.chat` 会写 user+assistant；
  `L1Orchestrator.mine` 会写 user（以及在 clarify 时写追问）。它们先于本层落地，
  本层再补一条就会重复。
- 本层只负责**能力没写的那一条**：L1 命中/空库的结果消息、知识录入、以及未接入能力的引导。

**卡片落在哪？** 只挂在「有结果」的回合上。L1 的追问、普通闲聊都是纯文本回合，不带卡——
脱开页面重新进来时，历史里能看到结论卡，看不到中间过程的候选列表，这是有意取舍：
候选是瞬时的，结论才值得留。

这些结果消息还有一个**结构性副作用**：它把 `source` 从 `l1` 换成了 `agent`，
于是下一轮挖掘的「连续澄清轮」计数会归零（见 `L1Orchestrator._l1_turn_count`）。
一条长会话里反复挖掘，追问预算才是各自独立的。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agent.l1_orchestrator import L1Orchestrator, L1Result
from app.agent.orchestrator import Orchestrator
from app.agent.router import CapabilityRoute, CapabilityRouter, capability_spec, parse_note
from app.domain.repositories.conversation_repository import ConversationRepository
from app.feedback import events
from app.ingestion.service import IngestionService
from app.llm.gateway import ModelGateway
from app.retrieval.embedding import build_embedding

logger = logging.getLogger(__name__)

# 本层追加的消息统一带这个来源标记，与 L1 自己的 source="l1" 区分开
SOURCE = "agent"
_TITLE_MAX = 256


# ---- 卡片构造 --------------------------------------------------------------
#
# 卡片是后端与前端之间的契约：前端只按 kind 渲染，不猜能力语义。


def _candidates_payload(result: L1Result) -> list[dict]:
    """召回候选原样带出（含已命中项）。

    沿用 L1 端点的既有约定：这是「本次召回的完整视图」，由调用方自行排除已命中的 id。
    只给一条结果时用户无从判断模型是真定位到了、还是随手挑了一条，所以候选必须带出来。
    """
    return [
        {
            "item_id": c.item_id,
            "title": c.title,
            "snippet": c.snippet,
            "score": c.score,
            "channels": list(c.channels),
        }
        for c in result.candidates
    ]


def _card_l1_located(result: L1Result) -> dict:
    return {
        "kind": "l1_located",
        "items": [
            {
                "item_id": item.item_id,
                "title": item.title,
                "snippet": item.snippet,
                "read_progress": round(float(item.read_progress or 0.0), 4),
                "embed_status": item.embed_status,
                "href": f"/knowledge.html?item={item.item_id}",
            }
            for item in result.located_items
        ],
        "candidates": _candidates_payload(result),
        "hint": result.read_hint,
        "reason": result.reason,
    }


def _card_l1_clarify(result: L1Result) -> dict:
    return {
        "kind": "l1_clarify",
        "question": result.question,
        "candidates": _candidates_payload(result),
        "turn": result.turn,
        "max_turns": result.max_turns,
        "reason": result.reason,
    }


def _card_l1_empty() -> dict:
    return {
        "kind": "l1_empty",
        "title": "知识库还是空的",
        "note": "先录入几条知识再来挖掘——没有素材就没法按线索找回。",
        "href": "/knowledge.html",
    }


def _card_knowledge_created(item) -> dict:
    return {
        "kind": "knowledge_created",
        "item_id": item.id,
        "title": item.title,
        "tags": [str(tag) for tag in (item.tags or [])],
        "embed_status": item.embed_status,
        "href": f"/knowledge.html?item={item.id}",
    }


def _card_note_empty() -> dict:
    return {
        "kind": "note_empty",
        "title": "没看出要记什么",
        "note": "换成「记一下：正文内容 #标签」的写法再试一次。",
    }


def _card_guide(capability: str) -> dict:
    """未接入对话的能力 → 一张把用户送回原页面的引导卡。

    刻意**不**复用能力自己的编排器去「顺手做一下」：那样用户以为是对话在做，
    出了问题也不知道该找哪个页面看结果。说清楚「还没接进来、先去哪儿」更诚实。
    """
    spec = capability_spec(capability)
    label = spec.label if spec else "这个能力"
    href = spec.href if spec else "/"
    note = (
        f"「{label}」还没接进对话，先去原页面用；接进来之后这里可以直接操作。"
        if href
        else f"「{label}」还没接进对话。"
    )
    return {
        "kind": "guide",
        "capability": capability,
        "label": label,
        "href": href,
        "note": note,
    }


@dataclass
class CopilotTurn:
    """一轮对话的结果。`card` 为空表示这是纯文本回合（闲聊、追问）。"""

    conversation_id: str
    capability: str
    reply: str
    card: dict | None = None
    state: str = "idle"
    decided_by: str = ""  # local | model | fallback

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "capability": self.capability,
            "reply": self.reply,
            "card": self.card,
            "state": self.state,
            "decided_by": self.decided_by,
        }


class Copilot:
    """对话入口的统一编排。`gateway` / `router` / `embedding` 均可注入（测试用假件）。"""

    def __init__(
        self,
        gateway: ModelGateway,
        session: Session,
        *,
        router: CapabilityRouter | None = None,
        embedding=None,
    ) -> None:
        self._gateway = gateway
        self._session = session
        self._router = router or CapabilityRouter(gateway, session)
        self._embedding = embedding

    # ---- 对外 ------------------------------------------------------------

    def handle(self, *, user_id: str, conversation_id: str | None, message: str) -> CopilotTurn:
        repo = ConversationRepository(self._session, user_id=user_id)
        conversation = self._load_or_create(repo, user_id, conversation_id)

        # 澄清态下不调模型分流：用户这句话极可能就是对追问的回答，
        # 再花一次调用猜意图既费钱又可能把澄清打断（命令式规则仍然生效）。
        clarifying = (conversation.state or "") == "clarifying"
        route = self._router.route(message, user_id=user_id, allow_model=not clarifying)

        method = self._handlers().get(route.capability)
        if method is None:
            reply, card = self._guide(
                repo, conversation, user_id=user_id, message=message, route=route
            )
        else:
            reply, card = method(
                repo, conversation, user_id=user_id, message=message, route=route
            )

        self._session.commit()
        self._emit_turn(user_id, route, card)
        return CopilotTurn(
            conversation_id=conversation.id,
            capability=route.capability,
            reply=reply,
            card=card,
            state=conversation.state or "idle",
            decided_by=route.source,
        )

    # ---- 分发 ------------------------------------------------------------

    def _handlers(self) -> dict[str, object]:
        """已接入的能力。未登记的能力一律走引导卡。"""
        return {
            "l1": self._l1,
            "knowledge_add": self._knowledge_add,
            "chat": self._chat,
        }

    # ---- 能力实现 --------------------------------------------------------

    def _chat(self, repo, conversation, *, user_id, message, route):
        """通用对话：消息由 Orchestrator 自己追加，本层不重复写。"""
        _conv, completion = Orchestrator(self._gateway, self._session).chat(
            user_id=user_id, conversation_id=conversation.id, message=message
        )
        return completion.text, None

    def _l1(self, repo, conversation, *, user_id, message, route):
        result = L1Orchestrator(self._gateway, self._session).mine(
            user_id=user_id, conversation_id=conversation.id, message=message
        )

        if result.state == "located":
            card = _card_l1_located(result)
            reply = _located_reply(result)
            # 结果消息由本层追加：L1 命中时自己只置状态、不写消息
            repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)
            return reply, card

        if result.state == "clarifying":
            # 追问由 L1 自己追加（source=l1），重复写会出现两条同样的提问。
            # 卡片只用于本轮即时渲染，不进历史——候选是瞬时的，结论才值得留。
            return result.question, _card_l1_clarify(result)

        card = _card_l1_empty()
        reply = result.question or card["note"]
        repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)
        return reply, card

    def _knowledge_add(self, repo, conversation, *, user_id, message, route):
        note = self._note_args(message, route)
        if not note["content"]:
            card = _card_note_empty()
            repo.append_message(conversation, "user", message, source=SOURCE)
            repo.append_message(conversation, "assistant", card["note"], source=SOURCE, card=card)
            return card["note"], card

        service = IngestionService(self._session, self._embedding or build_embedding())
        item = service.add_knowledge(
            user_id=user_id,
            title=note["title"],
            content=note["content"],
            source=SOURCE,
            tags=note["tags"],
        )
        card = _card_knowledge_created(item)
        reply = f"已记下：{item.title}"
        repo.append_message(conversation, "user", message, source=SOURCE)
        repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)
        return reply, card

    def _guide(self, repo, conversation, *, user_id, message, route):
        card = _card_guide(route.capability)
        repo.append_message(conversation, "user", message, source=SOURCE)
        repo.append_message(conversation, "assistant", card["note"], source=SOURCE, card=card)
        return card["note"], card

    # ---- 内部 ------------------------------------------------------------

    @staticmethod
    def _load_or_create(repo: ConversationRepository, user_id: str, conversation_id: str | None):
        """取会话，取不到就新建。

        两种「取不到」都要当新建处理，且**不能抛错**：

        - 空 id：第一次说话。
        - 越权 id：仓储的 `_guard` 会抛 PermissionError。会话入口把它降级成「开新会话」——
          浏览器里留着一个别的账号（或已注销账号）的会话 id 是很正常的过期状态，
          为此回 500 说不过去；而静默开新会话也不会泄漏任何数据（拿到的是一张空会话）。
        """
        if conversation_id:
            try:
                found = repo.get(conversation_id)
            except PermissionError:
                logger.warning("conversation id 越权或已失效，改为新开会话")
                found = None
            if found is not None:
                return found
        return repo.create(user_id=user_id)

    @staticmethod
    def _note_args(message: str, route: CapabilityRoute) -> dict:
        """取出要录入的 title / content / tags。

        模型分流时可能已经把 args 抽好了，优先用它；本地规则只判出「这是要记东西」，
        正文得自己剥。两条路径都必须能用——少一条就会在某条入口上记出空条目。
        """
        args = route.args or {}
        content = str(args.get("content") or "").strip()
        title = str(args.get("title") or "").strip()
        raw_tags = args.get("tags")
        tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else []

        if not content:
            parsed = parse_note(message)
            content = parsed["content"]
            title = title or parsed["title"]
            tags = tags or list(parsed["tags"])

        fallback_title = (content or "").strip().splitlines()[0] if content else ""
        return {
            "title": (title or fallback_title)[:_TITLE_MAX],
            "content": content,
            "tags": tags,
        }

    def _emit_turn(self, user_id: str, route: CapabilityRoute, card: dict | None) -> None:
        """埋点只记元数据：能力名、判定来源、置信度、卡片类型——正文与用户输入一律不进事件表。"""
        payload = {
            "capability": route.capability,
            "decided_by": route.source,
            "confidence": route.confidence,
            "card_kind": (card or {}).get("kind", ""),
        }
        events.record(self._session, user_id=user_id, event_type=events.AGENT_TURN, payload=payload)
        if route.source == "fallback":
            # 分流失败的样本留给规则调优：看到它对不上的那类说法，再补本地规则
            events.record(
                self._session,
                user_id=user_id,
                event_type=events.AGENT_ROUTE_MISSED,
                payload=payload,
            )


def _located_reply(result: L1Result) -> str:
    """命中后的那一句回复。标题列出来，细节交给卡片。"""
    if not result.located_items:
        return "没能定位到具体条目，换个说法再试试？"
    titles = "、".join(item.title for item in result.located_items[:3])
    reply = f"找到 {len(result.located_items)} 条：{titles}"
    if result.read_hint:
        reply = f"{reply}。{result.read_hint}"
    return reply
