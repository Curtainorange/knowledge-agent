"""统一对话编排：一句话进来，出去的是「一句回复 + 一张卡片」。

这是「对话即入口」的落点，它本身**不实现任何能力**，只做三件事：

1. **承接**：上一轮 L1 还在等澄清时，把这句话优先当成对追问的回答（见 router 的说明）。
2. **分发**：按 `CapabilityRoute` 把话交给对应的编排器 / 服务，或交给 `turns` 异步跑。
3. **收口**：把结果整理成统一卡片写回会话，前端只认卡片、不认能力细节。

模块分工（改之前先看清楚，免得把逻辑放错地方）：

- `router`   —— 一句话 → 一条 `CapabilityRoute`（意图判定）
- `turns`    —— 异步回合、结果写回、卡片内操作回流（耗时能力与可变状态）
- `cards`    —— 卡片协议与配套回复文案（前后端唯一契约）
- 本模块     —— 把上面三样拼起来，并决定「同步跑还是异步跑」

---

**谁负责追加消息？** 这里有一条容易踩的约定：

- 各能力**自己追加自己的消息**。`Orchestrator.chat` 会写 user+assistant；
  `L1Orchestrator.mine` 会写 user（以及在 clarify 时写追问）。它们先于本层落地，
  本层再补一条就会重复。
- 本层只负责**能力没写的那一条**：L1 命中/空库的结果消息、重能力的结果、
  知识录入、以及未接入能力的引导（统一走 `_append_exchange`）。

**同步还是异步？** `l2/l3/l5` 量级在十几秒到几分钟，交给 `turns` 入队后台跑
（前端先拿到一张 pending 卡）。但异步**依赖 worker**：`worker_enabled=False`
（测试、或某些只读部署）时后台没人干活，那样 pending 卡会永远转圈——
所以此时自动退回同步执行，慢，但一定有结果。

这些结果消息还有一个**结构性副作用**：它把 `source` 从 `l1` 换成了 `agent`，
于是下一轮挖掘的「连续澄清轮」计数会归零（见 `L1Orchestrator._l1_turn_count`）。
一条长会话里反复挖掘，追问预算才是各自独立的。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from app.agent import turns
from app.agent.cards import (
    guide_card,
    knowledge_created_card,
    l1_clarify_card,
    l1_empty_card,
    l1_located_card,
    located_reply,
    note_empty_card,
)
from app.agent.greeting import opening_for
from app.agent.l1_orchestrator import L1Orchestrator
from app.agent.orchestrator import Orchestrator
from app.agent.router import CapabilityRoute, CapabilityRouter, parse_note
from app.agent.turns import SOURCE
from app.core.config import settings
from app.domain.repositories.conversation_repository import ConversationRepository
from app.feedback import events
from app.ingestion.service import IngestionService
from app.llm.gateway import ModelGateway
from app.retrieval.embedding import build_embedding

logger = logging.getLogger(__name__)

_TITLE_MAX = 256


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

    def start(self, *, user_id: str, conversation_id: str | None) -> str:
        """开启或恢复一个会话，返回会话 id。

        **新会话由副驾先开口**（见 `greeting`）——用户还没说话就先接上话题，
        而不是对着一个空白输入框等人下指令。

        已经有消息的会话原样返回：刷新一次页面就多问候一句，是最容易被一眼看穿的
        「假智能」。判断「是不是新会话」用「有没有消息」而不是「有没有 id」——
        前端拿的 id 可能是过期或越权的，`_load_or_create` 会把它降级成新会话，
        那种情况同样是新会话、同样该开场。
        """
        repo = ConversationRepository(self._session, user_id=user_id)
        conversation = self._load_or_create(repo, user_id, conversation_id)
        if not (conversation.messages or []):
            repo.append_message(
                conversation,
                "assistant",
                opening_for(self._session, user_id=user_id),
                source=SOURCE,
            )
            self._session.commit()
        return conversation.id

    def handle(self, *, user_id: str, conversation_id: str | None, message: str) -> CopilotTurn:
        repo = ConversationRepository(self._session, user_id=user_id)
        conversation = self._load_or_create(repo, user_id, conversation_id)

        # 澄清态下不调模型分流：用户这句话极可能就是对追问的回答，
        # 再花一次调用猜意图既费钱又可能把澄清打断（命令式规则仍然生效）。
        clarifying = (conversation.state or "") == "clarifying"
        route = self._router.route(message, user_id=user_id, allow_model=not clarifying)

        if self._runs_async(route.capability):
            # 开跑前先确认这事现在做得成：前提不满足就同步回提示卡，
            # 别先发 pending 卡、让后台跑一趟才发现做不了（用户会白等一个轮询周期）
            ready = turns.preflight(route.capability, user_id=user_id, session=self._session)
            if ready is not None:
                reply, card = ready
                self._append_exchange(repo, conversation, message, reply, card)
            else:
                reply, card = turns.start_turn(
                    self._session, user_id=user_id, conversation=conversation,
                    capability=route.capability, message=message, args=route.args,
                )
        else:
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

    @staticmethod
    def _runs_async(capability: str) -> bool:
        """重能力走后台。**必须有 worker 才敢异步**——没有 worker 的话 pending 卡永远不落地。"""
        return capability in turns.ASYNC_CAPABILITIES and settings.worker_enabled

    def _handlers(self) -> dict[str, object]:
        """已接入的能力。未登记的能力一律走引导卡。

        除了 `l1` / `knowledge_add` / `chat`（它们要写自己的消息），其余能力都走
        `_execute`：执行逻辑统一在 `turns.execute_capability`，本层只负责把结果落成消息。
        `l2/l3/l5/l4_plan/l4_deviation` 正常路径会被 `_runs_async` 拦到后台去，
        这里只在没有 worker 时兜底同步跑。
        """
        return {
            "l1": self._l1,
            "l2": self._execute,
            "l3": self._execute,
            "l4_deviation": self._execute,
            "l4_goal": self._execute,
            "l4_plan": self._execute,
            "l5": self._execute,
            "books": self._execute,
            "knowledge_add": self._knowledge_add,
            "weread_sync": self._execute,
            # 通读/推荐走 turns.execute_capability；讨论需要用户那句话原文当问题，
            # 单独一个 handler（execute_capability 的 args 契约装不下它）
            "book_digest": self._execute,
            "book_recommend": self._execute,
            "book_discuss": self._book_discuss,
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
            card = l1_located_card(result)
            reply = located_reply(result)
            # 结果消息由本层追加：L1 命中时自己只置状态、不写消息
            repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)
            return reply, card

        if result.state == "clarifying":
            # 追问由 L1 自己追加（source=l1），重复写会出现两条同样的提问。
            # 卡片只用于本轮即时渲染，不进历史——候选是瞬时的，结论才值得留。
            return result.question, l1_clarify_card(result)

        card = l1_empty_card()
        reply = result.question or card["note"]
        repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)
        return reply, card

    def _execute(self, repo, conversation, *, user_id, message, route):
        """执行能力并落消息。异步能力只在没有 worker 时走到这里（否则已被 `_runs_async` 拦下）。"""
        reply, card = turns.execute_capability(
            route.capability,
            user_id=user_id,
            session=self._session,
            gateway=self._gateway,
            key=str(uuid4()),
            args=route.args,
        )
        self._append_exchange(repo, conversation, message, reply, card)
        return reply, card

    def _book_discuss(self, repo, conversation, *, user_id, message, route):
        """就通读过的书讨论一轮。消息由本层追加（执行体不写消息，与 `_execute` 同一分工）。"""
        reply, card = turns.execute_book_discuss(
            user_id=user_id,
            session=self._session,
            gateway=self._gateway,
            message=message,
            args=route.args,
        )
        self._append_exchange(repo, conversation, message, reply, card)
        return reply, card

    def _knowledge_add(self, repo, conversation, *, user_id, message, route):
        note = self._note_args(message, route)
        if not note["content"]:
            card = note_empty_card()
            self._append_exchange(repo, conversation, message, card["note"], card)
            return card["note"], card

        service = IngestionService(self._session, self._embedding or build_embedding())
        item = service.add_knowledge(
            user_id=user_id,
            title=note["title"],
            content=note["content"],
            source=SOURCE,
            tags=note["tags"],
        )
        card = knowledge_created_card(item)
        reply = f"已记下：{item.title}"
        self._append_exchange(repo, conversation, message, reply, card)
        return reply, card

    def _guide(self, repo, conversation, *, user_id, message, route):
        card = guide_card(route.capability)
        self._append_exchange(repo, conversation, message, card["note"], card)
        return card["note"], card

    # ---- 内部 ------------------------------------------------------------

    @staticmethod
    def _append_exchange(repo: ConversationRepository, conversation, message: str,
                         reply: str, card: dict | None = None) -> None:
        """给「自己什么都不写」的能力补上 user + assistant 两条消息。

        与 `_l1` / `_chat` 的分工是互补的：那两个能力自己写了消息，本层就不插手；
        这里的能力（L2/L3/L5、知识录入、引导卡）什么都没写，两条都得本层补。
        写重了历史里会冒出两条一样的回复，写漏了历史就断片——两个方向都有测试锁着。
        """
        repo.append_message(conversation, "user", message, source=SOURCE)
        repo.append_message(conversation, "assistant", reply, source=SOURCE, card=card)

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
