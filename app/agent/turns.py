"""异步能力回合 + 卡片内操作回流。

**为什么需要这一层。** L2 要逐条提主张、最多送 20 对进 LLM；L3 两次模型调用；
L5 是 reasoning=on 的链式归因——都是十几秒到几分钟的量级。压在 HTTP 请求里，
前端只能转圈，而浏览器与中间代理的超时随时可能把这次调用掐断，用户看到的是
「点了没反应」，也说不清到底做没做。

**做法（刻意不引入新表，也不引入 Celery / Redis）：**

1. 复用已有的任务队列（`app/workers`，自带幂等键 / 重试 / 死信）入队一个
   `agent_capability` 任务，幂等键就是 turn_id。
2. **状态落在会话消息上**：先在会话里写一条 `card.kind="pending"` 的 assistant 消息
   （带 turn_id），worker 执行完**就地把这条消息改成结果卡片**。这样做有两个好处：
   不需要把结果再塞去别的地方；页面重新打开时历史里看到的就是最终结果，
   而不是一条永远「进行中」的死消息。
3. 轮询用「重新读会话」（`read_conversation`）而不是单独的 turns 端点——
   顺带把「刷新页面历史全丢」这件事一起解决了。

**读取时会刷新两类卡片。** 消息里存的卡片是**快照**，而 L2 的 `user_state`、
L5 的 `status` 是会变的。若不刷新，重新打开页面会看到已经忽略过的冲突又回到「待处理」——
卡片在说谎。所以 `refresh_card` 对可操作卡片按数据库重新取值（纯读，不在 GET 里写库），
包括把卡死的 pending 卡按任务状态落成「失败」。

**操作不写新消息。** 采纳 / 忽略是对已有结果的处置，不是新的一轮对话；
写一条「已忽略」只会把消息流刷满噪声。动作结果由 `L2_CONFLICT_FEEDBACK` /
`L5_DIAGNOSIS_DECIDED` 两个学习事件记录（它们本来就是 L5/L4 的输入），
卡片自身则按状态刷新——这就是那份记录。
"""
from __future__ import annotations

import logging
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import cards as cards_mod
from app.agent.cards import (
    brief_reply,
    diagnosis_reply,
    failed_card,
    l2_conflicts_card,
    l3_brief_card,
    l5_diagnosis_card,
    pending_card,
    scan_reply,
)
from app.agent.l2_orchestrator import L2Orchestrator
from app.agent.l3_orchestrator import L3Orchestrator
from app.agent.l5_orchestrator import L5Orchestrator
from app.domain.models.task_run import TaskRun
from app.domain.repositories.conversation_repository import ConversationRepository
from app.llm.gateway import ModelGateway

logger = logging.getLogger(__name__)

# 对话层写入的消息统一带这个来源标记。
# 它不只是一个标签：`L1Orchestrator._l1_turn_count` 靠「非 l1 来源」把连续澄清轮计数归零，
# 所以这个值与 l1 的 source 必须始终不同。
SOURCE = "agent"

TASK_NAME = "agent_capability"
TASK_KEY_PREFIX = "agent:"

# 耗时长到必须异步的能力。其余能力（L1、知识录入、闲聊）都是秒级，同步更干脆。
ASYNC_CAPABILITIES: frozenset[str] = frozenset({"l2", "l3", "l5"})

_PENDING_COPY: dict[str, tuple[str, str]] = {
    "l2": ("正在扫描冲突", "逐条提取主张再两两判定，通常半分钟以内。结果会自动出现在这里。"),
    "l3": ("正在生成认知简报", "要读标题与摘要做归类、再生成追问，通常十几秒。"),
    "l5": ("正在做归因诊断", "要结合行为统计做链式归因，通常十几秒。"),
}


def task_key(turn_id: str) -> str:
    return f"{TASK_KEY_PREFIX}{turn_id}"


# ---- 能力执行（不含任何消息读写） ------------------------------------------
#
# 同步路径与异步路径共用这一份执行逻辑——这是「异步」不改变行为的前提：
# 两条路径只有「在哪儿跑」，没有「跑什么」的差别。


def execute_capability(
    capability: str, *, user_id: str, session: Session, gateway: ModelGateway, key: str
) -> tuple[str, dict]:
    """跑一次重能力，返回 (回复文案, 卡片)。

    只做执行，**不碰会话消息**：消息归属由调用方决定（同步路径写在当前请求里，
    异步路径写在 worker 里）。
    """
    if capability == "l2":
        result = L2Orchestrator(gateway, session).scan(user_id=user_id)
        summary = {
            "scanned_items": result.scanned_items,
            "claims_extracted": result.claims_extracted,
            "pairs_judged": result.pairs_judged,
            "conflicts_found": result.conflicts_found,
            "conflicts_suppressed": result.conflicts_suppressed,
            "extraction_failures": result.extraction_failures,
        }
        items = conflict_views(session, user_id=user_id, conflict_ids=result.conflict_ids)
        return scan_reply(summary), l2_conflicts_card(key=key, items=items, summary=summary)

    if capability == "l3":
        result = L3Orchestrator(gateway, session).brief(user_id=user_id)
        return brief_reply(result), l3_brief_card(result)

    if capability == "l5":
        result = L5Orchestrator(gateway, session).diagnose(user_id=user_id)
        return diagnosis_reply(result), l5_diagnosis_card(key=key, result=result)

    raise ValueError(f"不支持异步执行的能力：{capability}")


# ---- 冲突视图 --------------------------------------------------------------


def conflict_views(
    session: Session, *, user_id: str, conflict_ids: list[str] | None = None, limit: int = 50
) -> list[dict]:
    """冲突的展示视图（标题 / 主张 / 判定 / 当前状态）。

    与 `GET /api/v1/l2/conflicts` 的字段一一对应：同一份数据在对话卡片与原页面里
    长得一样，用户不必在两个地方学两套说法。
    """
    from app.domain.repositories.claim_repository import ClaimRepository
    from app.domain.repositories.conflict_repository import ConflictRepository
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    xrepo = ConflictRepository(session, user_id=user_id)
    krepo = KnowledgeRepository(session, user_id=user_id)
    crepo = ClaimRepository(session, user_id=user_id)

    if conflict_ids is None:
        rows = xrepo.list_by_user(user_id, limit=limit)
    else:
        rows = [row for row in (xrepo.get(cid) for cid in conflict_ids) if row is not None]

    views: list[dict] = []
    for row in rows:
        item_a = krepo.get(row.item_a_id)
        item_b = krepo.get(row.item_b_id)
        claim_a = crepo.get(row.claim_a_id) if row.claim_a_id else None
        claim_b = crepo.get(row.claim_b_id) if row.claim_b_id else None
        views.append({
            "conflict_id": row.id,
            "item_a_id": row.item_a_id,
            "item_b_id": row.item_b_id,
            "title_a": item_a.title if item_a else "（条目已删除）",
            "title_b": item_b.title if item_b else "（条目已删除）",
            "claim_a": claim_a.statement if claim_a else "",
            "claim_b": claim_b.statement if claim_b else "",
            "conflict_type": row.conflict_type,
            "detail": row.detail,
            "suggestion": row.suggestion,
            "confidence": row.confidence,
            "user_state": row.user_state,
        })
    return views


# ---- 异步回合 --------------------------------------------------------------


def start_turn(
    session: Session, *, user_id: str, conversation, capability: str, message: str
) -> tuple[str, dict]:
    """开一个异步回合：写 pending 消息 → 入队 → 叫醒 worker。

    顺序不能反：**先把 pending 消息提交，再入队**。反过来的话 worker 可能在消息
    落库之前就抢到任务、执行完却找不到那条消息，结果就丢了。
    """
    from app.workers import runner
    from app.workers.tasks import enqueue

    turn_id = str(uuid4())
    label, note = _PENDING_COPY.get(capability, ("正在处理", "结果稍后出现。"))
    card = pending_card(turn_id=turn_id, capability=capability, label=label, note=note)

    repo = ConversationRepository(session, user_id=user_id)
    repo.append_message(conversation, "user", message, source=SOURCE)
    repo.append_message(conversation, "assistant", note, source=SOURCE, card=card)
    session.commit()

    enqueue(
        session,
        task_name=TASK_NAME,
        user_id=user_id,
        idempotency_key=task_key(turn_id),
        payload={
            "turn_id": turn_id,
            "user_id": user_id,
            "conversation_id": conversation.id,
            "capability": capability,
        },
    )
    # 唤醒 worker：否则要等满一个轮询周期（默认 15s）才开工，
    # 「异步」就变成了纯粹的等待。
    runner.nudge()
    return note, card


def finish_turn(
    session: Session, *, user_id: str, conversation_id: str, turn_id: str, reply: str, card: dict
) -> bool:
    """把 pending 消息就地改成结果（worker 与同步兜底都走这里）。

    返回 False 表示那条消息已经不在了（会话被删、或用户开了新会话）——
    这不是错误，任务照常记成功，只是没有可写回的地方。
    """
    repo = ConversationRepository(session, user_id=user_id)
    try:
        conversation = repo.get(conversation_id)
    except PermissionError:
        logger.warning("agent turn 写回被拒（会话越权）turn=%s", turn_id)
        return False
    if conversation is None:
        return False

    messages = list(conversation.messages or [])
    for index, raw in enumerate(messages):
        if (raw.get("card") or {}).get("turn_id") == turn_id:
            updated = dict(raw)
            updated["content"] = reply
            updated["card"] = card
            messages[index] = updated
            conversation.messages = messages  # 整体重新赋值，JSON 列的原地修改不会被追踪
            session.commit()
            return True
    logger.warning("agent turn 未找到对应消息 turn=%s conv=%s", turn_id, conversation_id)
    return False


# ---- 读取与刷新 ------------------------------------------------------------


def read_conversation(session: Session, *, user_id: str, conversation_id: str) -> dict:
    """读出会话（消息 + 卡片），供前端渲染与轮询。

    纯读：卡片的刷新只影响**返回值**，不回写数据库——GET 不该有副作用，
    刷新结果每次都能重新算出来，也就没有必要存。

    会话不存在抛 `LookupError`、越权抛 `PermissionError`，由端点分别转 404 / 403。
    """
    repo = ConversationRepository(session, user_id=user_id)
    conversation = repo.get(conversation_id)
    if conversation is None:
        raise LookupError("会话不存在")

    messages = []
    for raw in conversation.messages or []:
        card = raw.get("card")
        messages.append({
            "role": raw.get("role", ""),
            "content": raw.get("content", ""),
            "source": raw.get("source", ""),
            "card": refresh_card(session, user_id=user_id, card=card) if card else None,
        })
    return {
        "conversation_id": conversation.id,
        "state": conversation.state or "idle",
        "messages": messages,
    }


def refresh_card(session: Session, *, user_id: str, card: dict) -> dict:
    """按当前数据库状态刷新一张卡片（必要时改写成失败卡）。"""
    kind = card.get("kind")

    if kind == cards_mod.PENDING_KIND:
        return _refresh_pending(session, user_id=user_id, card=card)

    if kind == "l2_conflicts":
        ids = [item.get("conflict_id") for item in (card.get("items") or [])]
        ids = [cid for cid in ids if cid]
        if not ids:
            return card
        refreshed = dict(card)
        refreshed["items"] = conflict_views(session, user_id=user_id, conflict_ids=ids)
        return refreshed

    if kind == "l5_diagnosis":
        diagnosis_id = card.get("diagnosis_id")
        if not diagnosis_id:
            return card
        from app.domain.repositories.cognitive_diagnosis_repository import (
            CognitiveDiagnosisRepository,
        )

        try:
            diagnosis = CognitiveDiagnosisRepository(session, user_id=user_id).get(diagnosis_id)
        except PermissionError:
            diagnosis = None
        if diagnosis is None:
            return card
        refreshed = dict(card)
        refreshed["status"] = diagnosis.status
        return refreshed

    return card


def _refresh_pending(session: Session, *, user_id: str, card: dict) -> dict:
    """卡住的 pending 卡不能永远转圈。

    任务进入死信（重试耗尽）时，把卡片换成失败卡并把重试入口指回原页面；
    否则用户只会看到一条永远「正在处理」的消息，既不知道出了事，也无处重试。
    """
    turn_id = str(card.get("turn_id") or "")
    if not turn_id:
        return card

    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == task_key(turn_id))
    ).first()
    if row is None or row.status in ("pending", "running"):
        return card

    capability = str(card.get("capability") or "")
    if row.status == "dead":
        note = "后台执行失败（可能网络或模型超时），可以换原页面手动重试。"
    else:
        note = "任务已结束，但结果没能写回这条消息，换原页面看一下最新结果。"
    return failed_card(turn_id=turn_id, capability=capability, note=note)


# ---- 操作回流 --------------------------------------------------------------


class ActionError(RuntimeError):
    """可以直接展示给用户的动作错误（卡片不存在、状态非法等）。"""


def apply_action(
    session: Session, *, user_id: str, conversation_id: str, card_key: str, action: str,
    target_id: str, value: str,
) -> tuple[str, dict]:
    """执行一次卡片内操作，返回 (提示文案, 刷新后的卡片)。

    前端拿到刷新后的卡片就地替换，读到的状态与数据库一致；
    历史里的快照则由 `refresh_card` 在下次读取时校正。
    """
    card = _find_card(session, user_id=user_id, conversation_id=conversation_id, card_key=card_key)
    if card is None:
        raise ActionError("这条卡片已不存在，刷新页面看看最新结果")

    kind = card.get("kind")
    if kind == "l2_conflicts" and action == "l2.conflict.state":
        reply = _set_conflict_state(session, user_id=user_id, conflict_id=target_id, state=value)
    elif kind == "l5_diagnosis" and action == "l5.diagnosis.decide":
        reply = _decide_diagnosis(session, user_id=user_id, diagnosis_id=target_id, value=value)
    else:
        raise ActionError(f"这张卡片不支持该操作（{kind} / {action}）")

    return reply, refresh_card(session, user_id=user_id, card=card)


def _find_card(session: Session, *, user_id: str, conversation_id: str, card_key: str) -> dict | None:
    try:
        conversation = ConversationRepository(session, user_id=user_id).get(conversation_id)
    except PermissionError as exc:
        raise ActionError("无权访问该会话") from exc
    if conversation is None:
        return None
    for raw in conversation.messages or []:
        card = raw.get("card") or {}
        if card.get("key") and card.get("key") == card_key:
            return card
    return None


def _set_conflict_state(session: Session, *, user_id: str, conflict_id: str, state: str) -> str:
    try:
        conflict = L2Orchestrator.decide_conflict(
            session, user_id=user_id, conflict_id=conflict_id, state=state
        )
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    except PermissionError as exc:
        raise ActionError("无权处理这条冲突") from exc
    if conflict is None:
        raise ActionError("这条冲突已经不在了")

    # 与 l2 端点同一套语义：忽略到一定次数后同类冲突不再产生推荐
    if state == "accepted":
        return "已采纳这条冲突的提示，后续会以它为准调整推荐"
    if state == "ignored":
        return "已忽略。同类冲突被反复忽略后，系统会减少这类推荐"
    return "已标回待处理"


def _decide_diagnosis(session: Session, *, user_id: str, diagnosis_id: str, value: str) -> str:
    accepted = value == "accepted"
    ok, message = L5Orchestrator(ModelGateway(), session).decide(
        user_id=user_id, diagnosis_id=diagnosis_id, accepted=accepted
    )
    if not ok:
        raise ActionError(message or "这条诊断已经不在了")
    return message
