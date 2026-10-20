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
from app.agent.conflict_view import conflict_views
from app.agent import book_orchestrator
from app.agent.cards import (
    books_card,
    books_reply,
    brief_reply,
    deviation_reply,
    diagnosis_reply,
    digest_reply,
    failed_card,
    goal_missing_card,
    goal_reply,
    l2_conflicts_card,
    l3_brief_card,
    l4_deviation_card,
    l4_goal_card,
    l4_plan_card,
    l5_diagnosis_card,
    book_digest_card,
    book_recommend_card,
    book_compare_card,
    comprehension_probe_card,
    insufficient_evidence_card,
    no_goal_card,
    notice_card,
    pending_card,
    plan_reply,
    reading_intake_card,
    recommend_reply,
    scan_reply,
    suggestion_card,
    sync_reply,
    weread_not_configured_card,
    weread_sync_card,
)
from app.agent.l2_orchestrator import L2Orchestrator
from app.agent.l3_orchestrator import L3Orchestrator
from app.agent.l4_orchestrator import L4Orchestrator
from app.agent.l5_orchestrator import L5Orchestrator
from app.core.config import settings
from app.domain.models.task_run import TaskRun
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.learning_plan_repository import LearningGoalRepository
from app.llm.gateway import ModelGateway

logger = logging.getLogger(__name__)

# 对话层写入的消息统一带这个来源标记。
# 它不只是一个标签：`L1Orchestrator._l1_turn_count` 靠「非 l1 来源」把连续澄清轮计数归零，
# 所以这个值与 l1 的 source 必须始终不同。
SOURCE = "agent"

TASK_NAME = "agent_turn"
TASK_KEY_PREFIX = "agent:"

# 耗时长到必须异步的能力。其余能力（L1、知识录入、闲聊）都是秒级，同步更干脆。
#
# 注意 `l4_*` 被拆成三个能力而不是一个 `l4`：**异步是按能力名判定的**，
# 而「定目标」（本地写库）与「偏离检查」（reasoning=on）差了一个数量级，
# 合成一个能力就没法给它们各自选同步还是异步。
ASYNC_CAPABILITIES: frozenset[str] = frozenset({
    "l2", "l3", "l5", "l4_plan", "l4_deviation", "weread_sync",
    # 书籍两项：推荐一次模型调用（十几秒），通读是「逐块读完全书」（分钟级）——
    # 都压不进一次 HTTP 请求，与 l2/l3 同一批
    "book_recommend", "book_digest",
    # 阅读建议：要召回 + 一次生成调用，十几秒；门禁（缺项）在 preflight 之前就拦掉了
    "suggestion",
    # 书籍对比：要读两本书的分章要点 + 一次 reasoning 调用
    "book_compare",
})

# 慢的卡片操作：跑模型、要十几秒，走后台，不在请求里等。
# 快操作（改状态、记偏好）留在 `/agent/actions` 同步返回。
SLOW_ACTIONS: frozenset[str] = frozenset({"l4.adjustment.apply"})

_PENDING_COPY: dict[str, tuple[str, str]] = {
    "l2": ("正在扫描冲突", "逐条提取主张再两两判定，通常半分钟以内。结果会自动出现在这里。"),
    "l3": ("正在生成认知简报", "要读标题与摘要做归类、再生成追问，通常十几秒。"),
    "l5": ("正在做归因诊断", "要结合行为统计做链式归因，通常十几秒。"),
    "l4_plan": ("正在拆解周计划", "要结合你的知识结构排任务，通常十几秒。"),
    "l4_deviation": ("正在检查计划执行", "先做本地统计，确认有偏离才做归因，通常十几秒。"),
    "weread_sync": ("正在同步微信读书", "逐本拉取划线与想法并逐条向量化，通常半分钟以内。"),
    "book_compare": ("正在对比这两本书", "先对齐主题、再逐条说差异，通常二十秒以内。"),
    "book_digest": ("正在通读这本书", "逐章读完再汇总，一本书要几分钟，读的过程不用你管。"),
    "book_recommend": ("正在挑书", "要结合你的知识库、书架和目标一起看，通常十几秒。"),
    "suggestion": ("正在按你的依据想建议", "只在你同步进来的内容里找，通常十几秒。"),
}

_ACTION_PENDING_COPY: dict[str, tuple[str, str]] = {
    "l4.adjustment.apply": ("正在按建议重排计划", "要重新拆一遍周任务，通常十几秒。"),
}


# ---- 前提检查 --------------------------------------------------------------
#
# 异步回合开跑**之前**要确认这件事现在做得成。前提不满足时同步回一张提示卡，
# 而不是先发 pending 卡、让后台跑一趟才发现做不了——那样用户会白等一个轮询周期，
# 先看到「正在同步…」再被告知「没配 Key」。


def preflight(capability: str, *, user_id: str, session: Session) -> tuple[str, dict] | None:
    """返回 (回复, 卡片) 表示「前提不满足，别开回合」；返回 None 表示可以开跑。"""
    if capability == "weread_sync":
        if not (settings.weread_api_key or "").strip():
            card = weread_not_configured_card()
            return card["note"], card

    if capability == "l4_plan":
        goals = LearningGoalRepository(session, user_id=user_id).list_active(user_id)
        if not goals:
            card = no_goal_card()
            return card["note"], card

    if capability == "l5":
        # E 模块：诊断前先做理解度探查。
        # 这不是"附加功能"，是诊断的前置条件——没有它，L5 只能拿行为计数推断
        # 「你没读懂」，而对已经懂了、只是懒得摘录的读者是彻底的误伤。
        return _l5_probe_preflight(session, user_id=user_id)

    return None


def _l5_probe_preflight(session: Session, *, user_id: str) -> tuple[str, dict] | None:
    """L5 的探查门禁。返回卡片表示"先别诊断"，返回 None 表示放行。"""
    from app.agent import probe

    state = probe.current_state(session, user_id=user_id)
    record = state.record

    if state.state == probe.STATE_NEED_QUESTION:
        # 有没答完的题：原样再摆一次，不换题（换题等于把上面的作答作废）
        if record is not None and record.question and not record.judge_key:
            step = "judge" if record.self_level else "level"
            card = comprehension_probe_card(
                key="probe", probe_id=record.id, step=step,
                topic=record.topic, question=record.question,
                options=record.options or [],
                note="先把这两步做完，我再判断该不该给你出诊断。",
            )
            return card["note"], card

        draft = probe.build_probe(session, user_id=user_id)
        if draft is None:
            # 出题失败就放行：辅助步骤不该把主功能卡死（降级方向与全项目一致）
            return None
        created = probe.save_question(session, user_id=user_id, draft=draft)
        card = comprehension_probe_card(
            key="probe", probe_id=created.id, step="level",
            topic=created.topic, question=created.question,
            options=created.options or [],
            note="先确认一下你的基础，再看一道小题。",
        )
        return "先做两道小题，我才知道该不该给你出诊断。", card

    if state.state == probe.STATE_NEED_RECALL and record is not None:
        card = comprehension_probe_card(
            key="probe", probe_id=record.id, step="recall",
            topic=record.topic, question=record.question,
            note="你选的是「读过 / 能讲」，但那道题答错了——用一句话说说它是什么，我再看。",
        )
        return "再用一句话说说它是什么，我就知道该不该出诊断了。", card

    if state.verdict == probe.VERDICT_GUIDE:
        card = notice_card(
            title="先读，不用诊断",
            note=(
                "这块你说是第一次接触——先把内容读一遍，把卡住的地方记下来，"
                "比现在做诊断有用。读完之后再来，我会问你另一道题。"
            ),
        )
        return card["note"], card

    return None


def task_key(turn_id: str) -> str:
    return f"{TASK_KEY_PREFIX}{turn_id}"


# ---- 能力执行（不含任何消息读写） ------------------------------------------
#
# 同步路径与异步路径共用这一份执行逻辑——这是「异步」不改变行为的前提：
# 两条路径只有「在哪儿跑」，没有「跑什么」的差别。


def execute_capability(
    capability: str, *, user_id: str, session: Session, gateway: ModelGateway, key: str,
    args: dict | None = None, conversation_id: str = "",
) -> tuple[str, dict]:
    """跑一次能力，返回 (回复文案, 卡片)。

    只做执行，**不碰会话消息**：消息归属由调用方决定（同步路径写在当前请求里，
    异步路径写在 worker 里）。

    `args` 只有 `l4_goal` 用得到（定目标 / 查看，二选一）。默认取**只读分支**：
    分流模型若漏给了 intent，宁可给用户看现状，也不要凭猜建一个错的目标——
    这与「分流失败回落通用对话」是同一条保守原则。

    `conversation_id` 给需要读会话状态的能力用（目前只有 `suggestion`：
    它要读 `reading_context` 里的确认结果）。其余能力忽略它。
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
            "book_readings_used": result.book_readings_used,
            "echoes": result.echoes,
        }
        items = conflict_views(session, user_id=user_id, conflict_ids=result.conflict_ids)
        return scan_reply(summary), l2_conflicts_card(key=key, items=items, summary=summary)

    if capability == "l3":
        result = L3Orchestrator(gateway, session).brief(user_id=user_id)
        return brief_reply(result), l3_brief_card(result)

    if capability == "l5":
        # 探查门禁在这里再判一次：`preflight` 只在异步路径（有 worker）上跑，
        # 无 worker 的降级路径直接进本函数——门禁若只写在 preflight，
        # 就会因为"部署方式不同"而失效。幂等（已出过题不会重复出），所以两边都判是安全的。
        gate = _l5_probe_preflight(session, user_id=user_id)
        if gate is not None:
            return gate
        result = L5Orchestrator(gateway, session).diagnose(user_id=user_id)
        if result.state in ("mastered", "bias", "guide"):
            # 这三类是"探查已经说明不必出缺陷"。用提示卡而不是诊断卡：
            # 套一张诊断卡却写着"没问题"，用户会以为诊断没跑成功。
            card = notice_card(title=_l5_soft_title(result.state), note=result.note)
            return result.note, card
        return diagnosis_reply(result), l5_diagnosis_card(key=key, result=result)

    if capability == "l4_goal":
        return _execute_l4_goal(user_id=user_id, session=session, key=key, args=args)

    if capability == "l4_plan":
        return _execute_l4_plan(user_id=user_id, session=session, gateway=gateway, key=key)

    if capability == "l4_deviation":
        return _execute_l4_deviation(user_id=user_id, session=session, gateway=gateway, key=key)

    if capability == "weread_sync":
        return _execute_weread_sync(user_id=user_id, session=session, key=key)

    if capability == "books":
        return _execute_books(user_id=user_id, session=session, key=key)

    if capability == "book_digest":
        return _execute_book_digest(
            user_id=user_id, session=session, gateway=gateway, key=key, args=args
        )

    if capability == "book_recommend":
        result = book_orchestrator.recommend(session, gateway, user_id=user_id)
        return recommend_reply(result), book_recommend_card(key=key, result=result)

    if capability == "suggestion":
        return _execute_suggestion(
            user_id=user_id, session=session, gateway=gateway, key=key,
            conversation_id=conversation_id, args=args or {},
        )

    if capability == "book_compare":
        return _execute_book_compare(
            user_id=user_id, session=session, gateway=gateway, key=key, args=args or {},
        )

    raise ValueError(f"不支持执行的能力：{capability}")


def _execute_suggestion(
    *, user_id: str, session: Session, gateway: ModelGateway, key: str,
    conversation_id: str, args: dict,
) -> tuple[str, dict]:
    """生成阅读建议。

    三层判定，任何一层不过都不出建议：
    1. 读不到会话 → 提示重来（没有确认结果，生成了也是猜）；
    2. 确认项没齐 → **退回确认卡**（这是门禁，不是失败）；
    3. 依据不够 → 说明缺什么并给三条出路（`grounding` 侧的判定）。

    只有三层都过，才真的调模型。**先判定后花钱**是这段代码的全部要点。
    """
    from app.agent import intake
    from app.agent import suggestion as sugg

    conversation = None
    if conversation_id:
        try:
            conversation = ConversationRepository(session, user_id=user_id).get(conversation_id)
        except PermissionError:
            conversation = None

    if conversation is None:
        card = insufficient_evidence_card(
            key=key, reason="这次没读到你的确认信息，重新开一个对话就好。",
        )
        return card["note"], card

    ctx = intake.load(session, conversation=conversation, user_id=user_id)

    if not ctx.is_ready:
        card = reading_intake_card(
            key="intake",
            round=ctx.round,
            inferred={"book": ctx.book, "basis": "system"},
            questions=intake.next_questions(ctx),
            digest_payload=intake.digest(ctx),
            note="先把这几项确认一下，我才能给到对你有用的建议。",
        )
        intake.mark_card_sent(ctx)
        intake.save(session, conversation=conversation, ctx=ctx)
        return intake.confirm_text(ctx), card

    outcome = sugg.generate(session, user_id=user_id, ctx=ctx, gateway=gateway)

    if outcome.state in ("insufficient_evidence", "restrained"):
        # 收敛（用户否过同类）与"没依据"是同一类结果：这次不给建议。
        # 用同一种卡片呈现，避免前端为它单独加一条渲染分支。
        card = insufficient_evidence_card(key=key, reason=outcome.note, hints=outcome.hints)
        intake.save(session, conversation=conversation, ctx=ctx)
        return outcome.note, card

    if outcome.state == "degraded":
        card = suggestion_card(
            key=key, state="degraded", digest_payload=outcome.digest,
            items=[], evidence=[ev.as_card() for ev in outcome.evidence],
            note=outcome.note,
        )
        intake.save(session, conversation=conversation, ctx=ctx)
        return outcome.note, card

    card = suggestion_card(
        key=key,
        state=outcome.state,
        digest_payload=outcome.digest,
        items=outcome.items,
        evidence=[ev.as_card() for ev in outcome.evidence],
        references=outcome.references,
        confidence=outcome.confidence,
        note=outcome.note,
    )
    # 记「已出过建议」：下次进页面不再从头问一遍（但槽位保留，用户改口仍可覆盖）
    intake.mark_ready_for_suggestion(ctx)
    intake.save(session, conversation=conversation, ctx=ctx)
    return sugg.reply_text(outcome), card


def _l5_soft_title(state: str) -> str:
    return {
        "mastered": "这块你已经掌握了",
        "bias": "方向对，说法有偏差",
        "guide": "先读，不用诊断",
    }.get(state, "诊断")


def _resolve_book_id(session: Session, *, user_id: str, title: str) -> str:
    """按书名找书：先精确匹配，再包含匹配。找不到返回空串。

    不做模糊编辑距离匹配：书架上两本名字相近的书（《原则》与《原则（实践版）》）
    被张冠李戴做对比，比"没找到"更糟——用户会拿去用。
    """
    from app.domain.models.book import Book

    wanted = (title or "").strip()
    if not wanted:
        return ""
    rows = session.scalars(
        select(Book).where(Book.user_id == user_id, Book.is_deleted.is_(False))
    ).all()
    for book in rows:
        if (book.title or "").strip() == wanted:
            return book.id
    for book in rows:
        current = (book.title or "").strip()
        if current and (wanted in current or current in wanted):
            return book.id
    return ""


def _execute_book_compare(
    *, user_id: str, session: Session, gateway: ModelGateway, key: str, args: dict
) -> tuple[str, dict]:
    """两本书逐主题对比。用户没说书名时用最近通读的两本，并在卡里说明用的是哪两本。"""
    from app.agent import book_compare

    title_a = str(args.get("book_a") or "").strip()
    title_b = str(args.get("book_b") or "").strip()

    book_a_id = _resolve_book_id(session, user_id=user_id, title=title_a)
    book_b_id = _resolve_book_id(session, user_id=user_id, title=title_b)

    unresolved = title_a if (title_a and not book_a_id) else (title_b if (title_b and not book_b_id) else "")
    if unresolved:
        card = book_compare_card(
            key=key, state="not_found",
            note=f"《{unresolved}》没找到——说准确书名，或者先把书加进书架。",
        )
        return card["note"], card

    if not book_a_id or not book_b_id:
        book_a_id, book_b_id = book_compare.pick_default_pair(session, user_id=user_id)
        if not book_a_id:
            card = book_compare_card(
                key=key, state="not_read",
                note="还没有两本通读过的书。先把书加进书架、让我读一遍，再来对比。",
            )
            return card["note"], card

    outcome = book_compare.compare(
        session, user_id=user_id, book_a_id=book_a_id, book_b_id=book_b_id, gateway=gateway,
    )
    card = book_compare_card(
        key=key, state=outcome.state,
        book_a=outcome.book_a, book_b=outcome.book_b,
        overview=outcome.overview, rows=outcome.rows,
        only_a=outcome.unmatched_a, only_b=outcome.unmatched_b,
        note=outcome.note,
    )
    return book_compare.reply_text(outcome), card


def _execute_book_digest(
    *, user_id: str, session: Session, gateway: ModelGateway, key: str, args: dict | None
) -> tuple[str, dict]:
    """通读一本书。书名对不上时出选择卡，让用户点一下书名再说一遍。"""
    from app.agent.router import parse_book_title

    payload = args or {}
    title = str(payload.get("title") or "").strip()
    force = bool(payload.get("force"))

    from app.domain.repositories.book_repository import BookRepository

    if not title:
        shelf = BookRepository(session, user_id=user_id).list_active(user_id)
        if not shelf:
            card = notice_card(
                title="书架还是空的",
                note="先去书架页上传一本电子书，回来再说「通读《书名》」。",
                href="/books.html",
            )
            return card["note"], card
        card = notice_card(
            title="要通读哪一本？",
            note="点下面任意一本开始；或者直接说「通读《书名》」。",
            sends=[
                {"label": f"通读《{book.title}》", "message": f"通读《{book.title}》"}
                for book in shelf[:5]
            ],
        )
        return card["note"], card

    book = book_orchestrator.resolve_book(session, user_id=user_id, title=title)
    if book is None:
        shelf = BookRepository(session, user_id=user_id).list_active(user_id)
        if not shelf:
            card = notice_card(
                title=f"书架里没有《{title}》",
                note="先去书架页上传这本电子书，再来通读。",
                href="/books.html",
            )
        else:
            card = notice_card(
                title=f"书架里没找到《{title}》",
                note="是下面某一本书的话，点它就开始；名字不在书架里就先去上传。",
                sends=[
                    {"label": f"通读《{b.title}》", "message": f"通读《{b.title}》"}
                    for b in shelf[:5]
                ],
            )
        return card["note"], card

    outcome = book_orchestrator.read_whole_book(
        session, gateway, user_id=user_id, book=book, force=force
    )
    return digest_reply(outcome), book_digest_card(key=key, outcome=outcome)


def execute_book_discuss(
    *, user_id: str, session: Session, gateway: ModelGateway, message: str, args: dict | None
) -> tuple[str, dict]:
    """就通读过的书讨论一轮（同步：一次模型调用，自然语言回复）。

    与 `execute_capability` 分开：它需要**用户那句话原文**当问题——
    本地规则只判得出「这是在聊书」，问题本身没法抽成结构化 args。
    """
    from app.agent.router import parse_book_title
    from app.domain.repositories.book_repository import BookRepository

    payload = args or {}
    title = str(payload.get("title") or "").strip() or parse_book_title(message)
    question = (message or "").strip()

    book = book_orchestrator.resolve_book(session, user_id=user_id, title=title)
    if book is None:
        card = notice_card(
            title="没确定要聊哪本书",
            note="带上书名再说一次，例如「聊聊《三体》」；也可以先「通读《书名》」。",
            href="/books.html",
        )
        return card["note"], card

    answer = book_orchestrator.discuss(
        session, gateway, user_id=user_id, book=book, question=question
    )
    if answer is None:
        card = notice_card(
            title=f"《{book.title}》还没通读过",
            note="先让我通读一遍再聊，讨论才有依据。",
            sends=[{"label": f"通读《{book.title}》", "message": f"通读《{book.title}》"}],
        )
        return card["note"], card
    return answer, None


def _execute_l4_goal(*, user_id: str, session: Session, key: str, args: dict | None) -> tuple[str, dict]:
    """定目标 / 查看当前目标与计划。两条路径都是本地操作，不调模型。"""
    payload = args or {}
    intent = str(payload.get("intent") or "").strip()
    goal_text = str(payload.get("goal") or "").strip()
    # gateway 留空：这两条路径都不调模型，惰性构造省一次 provider 构造
    orch = L4Orchestrator(session=session)

    if intent == "create" or goal_text:
        if not goal_text:
            card = goal_missing_card()
            return card["note"], card
        goal_id = orch.create_goal(user_id=user_id, description=goal_text)
        return goal_reply(goal_text), l4_goal_card(goal_id=goal_id, description=goal_text)

    view = orch.latest_plan_view(user_id=user_id)
    if view is None:
        goals = orch.list_goals(user_id=user_id)
        if goals:
            card = notice_card(
                title="还没有周计划",
                note=f"已有目标「{goals[0]['description']}」，说一句「生成周计划」就能拆成每周要做的事。",
                sends=[{"label": "生成周计划", "message": "生成周计划"}],
            )
            return card["note"], card
        card = no_goal_card()
        return card["note"], card

    card = l4_plan_card(
        key=key, state="ok", goal_description=view.goal_description, plan_id=view.plan_id,
        version=view.version, rationale=view.rationale, tasks=view.tasks, progress=view.progress,
        rules_snapshot=getattr(view, "rules_snapshot", {}) or {},
    )
    total = int(view.progress.get("total") or 0)
    done = int(view.progress.get("done") or 0)
    return f"当前计划 v{view.version}：{total} 项任务，已完成 {done} 项", card


def _execute_l4_plan(
    *, user_id: str, session: Session, gateway: ModelGateway, key: str
) -> tuple[str, dict]:
    """拆解周计划。作用于**最近创建的那个目标**。

    不做「问用户是哪个目标」：多目标场景下追问一轮的价值远低于直接作用于最新的那个，
    而卡上带着目标描述，拆错了用户一眼能看出来并重说一句。
    """
    goals = LearningGoalRepository(session, user_id=user_id).list_active(user_id)
    if not goals:
        card = no_goal_card()
        return card["note"], card

    goal = goals[0]
    result = L4Orchestrator(gateway, session).generate_plan(user_id=user_id, goal_id=goal.id)
    if result is None:
        card = no_goal_card()
        return card["note"], card

    view = result.view
    if view is None:
        card = l4_plan_card(
            key=key, state="degraded", goal_description=goal.description, note=result.note
        )
        return plan_reply("degraded", tasks=[], progress={}, note=result.note), card

    card = l4_plan_card(
        key=key, state=result.state, goal_description=view.goal_description,
        plan_id=view.plan_id, version=view.version, rationale=view.rationale,
        tasks=view.tasks, progress=view.progress, note=result.note,
        rules_snapshot=getattr(view, "rules_snapshot", {}) or {},
    )
    return plan_reply(result.state, tasks=view.tasks, progress=view.progress, note=result.note), card


def _execute_l4_deviation(
    *, user_id: str, session: Session, gateway: ModelGateway, key: str
) -> tuple[str, dict]:
    report = L4Orchestrator(gateway, session).check_deviation(user_id=user_id)

    signals: dict = {}
    if report.signals:
        s = report.signals
        signals = {
            "reasons": list(s.reasons),
            "window_days": s.window_days,
            "idle_days": s.idle_days,
            "recent_events": s.recent_events,
            "previous_events": s.previous_events,
            "plan_total": s.plan_total,
            "plan_done": s.plan_done,
            "topic_overlap": round(float(s.topic_overlap or 0.0), 3),
        }
    analysis: dict = {}
    if report.analysis:
        a = report.analysis
        analysis = {
            "root_cause": a.root_cause,
            "adjustment": a.adjustment,
            "expected_gain": a.expected_gain,
            "confidence": a.confidence,
        }

    card = l4_deviation_card(
        key=key, state=report.state, plan_id=report.plan_id,
        signals=signals, analysis=analysis,
        # ok 时 note 只是「以下是归因」这种过程话术，卡片本身已经把结论摆出来了；
        # 留空才能让 note 在卡片上专指「失败原因」（见 _refresh_pending 的还原路径）
        note=report.note if report.state != "ok" else "",
    )
    reply = deviation_reply(
        report.state, reasons=signals.get("reasons", []), analysis=analysis, note=report.note
    )
    return reply, card


def _execute_weread_sync(*, user_id: str, session: Session, key: str) -> tuple[str, dict]:
    """把微信读书的划线 / 想法同步进知识库。

    幂等由同步服务自己保证（`source_item_id` 去重，且**软删的条目也算已存在**），
    所以「再同步一次」永远是安全的——卡上那个「继续同步剩下的 N 本」按钮就靠这一点。
    """
    from app.weread.service import WeReadSyncService

    if not (settings.weread_api_key or "").strip():
        card = weread_not_configured_card()
        return card["note"], card

    result = WeReadSyncService(session).sync(user_id=user_id).as_dict()
    return sync_reply(result), weread_sync_card(key=key, result=result)


def _execute_books(*, user_id: str, session: Session, key: str) -> tuple[str, dict]:
    """看书架。纯本地查询，同步即可（不上异步）。

    上传不进对话：那要选本地文件，对话里做不到——卡上把这一点说清楚并给出书架页链接。
    """
    from app.domain.repositories.book_repository import BookRepository

    rows = BookRepository(session, user_id=user_id).list_active(user_id)
    books = [
        {
            "book_id": book.id,
            "title": book.title,
            "author": book.author or "",
            "format": book.format or "",
            # chapter_count 不是列，是端点里按 chapters 现算的——这里同样现算
            "chapter_count": len(book.chapters or []),
            "read_progress": round(float(book.read_progress or 0.0), 4),
            # 阅读器的入口参数是 ?book=（books.html 就是这么跳的），别写成 ?id=
            "href": f"/reader.html?book={book.id}",
        }
        for book in rows
    ]
    return books_reply(books), books_card(key=key, books=books)


def execute_action(
    action: str, *, user_id: str, session: Session, card_key: str, target_id: str, value: str,
    gateway: ModelGateway | None = None,
) -> tuple[str, dict]:
    """慢动作的实际执行体（worker 与同步兜底共用）。

    产出卡片的 `key` 固定用发起时那张卡的 key，这样前端 `data-card-key` 锚点在整个
    「pending → 结果」过程中保持不变——否则卡片一被替换，第二次点击就找不到锚点了。

    `gateway` 可注入：同步兜底路径（没有 worker）跑在请求线程里，走的是端点的依赖，
    注入得进来才测得动；后台路径由 worker 自建（与既有 l2_scan 处理器同一形态）。
    """
    if action == "l4.adjustment.apply":
        orch = L4Orchestrator(gateway or ModelGateway(), session)
        ok, message = orch.decide_adjustment(user_id=user_id, plan_id=target_id, accepted=True)
        if not ok:
            raise ActionError(message or "计划不存在")
        view = L4Orchestrator(session=session).latest_plan_view(user_id=user_id)
        if view is None:
            raise ActionError("重排后没有拿到计划")
        card = l4_plan_card(
            key=card_key, state="ok", goal_description=view.goal_description,
            plan_id=view.plan_id, version=view.version, rationale=view.rationale,
            tasks=view.tasks, progress=view.progress,
        )
        return message, card

    raise ValueError(f"不支持的动作：{action}")


# ---- 异步回合 --------------------------------------------------------------


def start_turn(
    session: Session, *, user_id: str, conversation, capability: str, message: str,
    args: dict | None = None,
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
            "kind": "capability",
            "turn_id": turn_id,
            "user_id": user_id,
            "conversation_id": conversation.id,
            "capability": capability,
            "args": dict(args or {}),
        },
    )
    # 唤醒 worker：否则要等满一个轮询周期（默认 15s）才开工，
    # 「异步」就变成了纯粹的等待。
    runner.nudge()
    return note, card


def start_action_turn(
    session: Session, *, user_id: str, conversation_id: str, card_key: str,
    action: str, target_id: str, value: str,
) -> tuple[str, dict]:
    """把一个**慢的**卡片操作放后台跑。

    与 `start_turn` 的差别只有一处：它不追加新消息，而是把**那张卡原地换成 pending 卡**，
    并把原卡存进 `restore`。这样两个方向都不吃亏——用户不会因为一次失败丢掉已经拿到的结果，
    也不会在消息流里多出一条其实什么都没说的「用户消息」。
    """
    from app.workers import runner
    from app.workers.tasks import enqueue

    original = _find_card(
        session, user_id=user_id, conversation_id=conversation_id, card_key=card_key
    )
    if original is None:
        raise ActionError("这条卡片已不存在，刷新页面看看最新结果")

    turn_id = str(uuid4())
    label, note = _ACTION_PENDING_COPY.get(action, ("正在处理", "结果稍后出现。"))
    pending = pending_card(turn_id=turn_id, capability="l4", label=label, note=note)
    pending["key"] = card_key          # 同一个 key：前端原地替换时锚点不变
    pending["restore"] = original      # 任务失败时原样还原
    pending["action"] = {"name": action, "target_id": target_id, "value": value}

    if not _replace_card(
        session, user_id=user_id, conversation_id=conversation_id, card_key=card_key, card=pending
    ):
        raise ActionError("这条卡片已不存在，刷新页面看看最新结果")
    session.commit()

    enqueue(
        session,
        task_name=TASK_NAME,
        user_id=user_id,
        idempotency_key=task_key(turn_id),
        payload={
            "kind": "action",
            "turn_id": turn_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "action": action,
            "card_key": card_key,
            "target_id": target_id,
            "value": value,
        },
    )
    runner.nudge()
    return note, pending


def _replace_card(
    session: Session, *, user_id: str, conversation_id: str, card_key: str, card: dict
) -> bool:
    """把会话里某张卡片的 `card` 字段换掉（消息本身与正文不动）。"""
    repo = ConversationRepository(session, user_id=user_id)
    try:
        conversation = repo.get(conversation_id)
    except PermissionError:
        return False
    if conversation is None:
        return False

    messages = list(conversation.messages or [])
    for index, raw in enumerate(messages):
        existing = raw.get("card") or {}
        if existing.get("key") and existing.get("key") == card_key:
            updated = dict(raw)
            updated["card"] = card
            messages[index] = updated
            conversation.messages = messages
            return True
    return False


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

    若是**卡片操作**发起的回合（pending 卡里存了 `restore`），则把原卡还原回去并附上
    失败原因——用户至少还是拿着那份结果，可以再点一次，而不是凭空少了一张卡。
    """
    turn_id = str(card.get("turn_id") or "")
    if not turn_id:
        return card

    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == task_key(turn_id))
    ).first()
    if row is None or row.status in ("pending", "running"):
        return card

    if row.status == "dead":
        note = "后台执行失败（可能网络或模型超时），可以再试一次。"
    else:
        note = "任务已结束，但结果没能写回这条消息，可以再试一次。"

    restore = card.get("restore")
    if isinstance(restore, dict) and restore:
        restored = dict(restore)
        restored["note"] = note          # 让卡片自己把失败原因说出来
        return restored

    return failed_card(
        turn_id=turn_id, capability=str(card.get("capability") or ""), note=note
    )


# ---- 操作回流 --------------------------------------------------------------


class ActionError(RuntimeError):
    """可以直接展示给用户的动作错误（卡片不存在、状态非法等）。"""


def apply_action(
    session: Session, *, user_id: str, conversation_id: str, card_key: str, action: str,
    target_id: str, value: str, gateway: ModelGateway | None = None,
) -> tuple[str, dict]:
    """执行一次卡片内操作，返回 (提示文案, 刷新后的卡片)。

    两类动作走两条路，分界线是**耗时**：

    - **快操作**（改状态、记偏好）：同步执行，立即返回刷新后的卡片。
    - **慢操作**（要跑模型，如按建议重排计划）：交给后台，先返回 pending 卡占位，
      跑完由 `_refresh_pending` 那条链路就地换成结果卡。没有 worker 时退回同步执行
      （慢，但一定有结果）。

    前端拿到返回的卡片就地替换，读到的状态与数据库一致；
    历史里的快照则由 `refresh_card` 在下次读取时校正。
    """
    card = _find_card(session, user_id=user_id, conversation_id=conversation_id, card_key=card_key)
    if card is None:
        raise ActionError("这条卡片已不存在，刷新页面看看最新结果")

    if action in SLOW_ACTIONS and settings.worker_enabled:
        return start_action_turn(
            session, user_id=user_id, conversation_id=conversation_id,
            card_key=card_key, action=action, target_id=target_id, value=value,
        )

    kind = card.get("kind")
    # 确认卡与建议卡的「答一题 / 否一条」会产出一张**新卡**（不是按数据库刷新旧卡）。
    # 新卡必须写回会话：否则前端内存里换掉了、刷新页面又变回旧的那张——
    # 用户会以为自己的选择没生效。其它卡片的状态变化由 `refresh_card` 在读时校正。
    if kind in ("reading_intake", "suggestion"):
        if kind == "reading_intake" and action == "intake.answer":
            reply, new_card = _apply_intake_answer(
                session, user_id=user_id, conversation_id=conversation_id,
                card=card, slot=target_id, value=value,
            )
        elif kind == "reading_intake" and action == "intake.skip":
            reply, new_card = _apply_intake_skip(
                session, user_id=user_id, conversation_id=conversation_id, card=card
            )
        elif kind == "suggestion" and action == "suggestion.reject":
            reply, new_card = _reject_suggestion(
                session, user_id=user_id, card=card, item_id=target_id
            )
        else:
            raise ActionError(f"这张卡片不支持该操作（{kind} / {action}）")
        # 新卡写回会话（复用 start_action_turn 用的那个 `_replace_card`）：
        # 不写回的话，前端内存里换掉了、刷新页面又变回旧的那张，用户以为选择没生效。
        _replace_card(
            session, user_id=user_id, conversation_id=conversation_id,
            card_key=card.get("key") or card_key, card=new_card,
        )
        session.commit()
        return reply, new_card

    if kind == "comprehension_probe" and action == "probe.level":
        # level 走 target_id（与 probe.answer 传 option_key 一致）。
        # 曾经误写成 value：前端把选项值放在 data-target、value 传空，
        # 于是接触史按钮点了必定 422——「按钮存在但点不动」的又一次。
        return _probe_set_level(
            session, user_id=user_id, conversation_id=conversation_id,
            card=card, level=target_id,
        )
    if kind == "comprehension_probe" and action == "probe.answer":
        return _probe_answer(
            session, user_id=user_id, conversation_id=conversation_id,
            card=card, option_key=target_id,
        )

    if kind == "l2_conflicts" and action == "l2.conflict.state":
        reply = _set_conflict_state(session, user_id=user_id, conflict_id=target_id, state=value)
    elif kind == "l5_diagnosis" and action == "l5.diagnosis.decide":
        reply = _decide_diagnosis(session, user_id=user_id, diagnosis_id=target_id, value=value)
    elif kind == "l4_deviation" and action == "l4.adjustment.keep":
        reply = _keep_adjustment(session, user_id=user_id, plan_id=target_id)
    elif kind == "l4_deviation" and action == "l4.adjustment.apply":
        # 走到这里说明没有 worker：同步跑完直接给结果卡
        return execute_action(
            action, user_id=user_id, session=session, card_key=card_key,
            target_id=target_id, value=value, gateway=gateway,
        )
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


# ---- 需求确认与建议的卡片操作（B 模块）-------------------------------------
#
# 这些操作都是**秒级**的：改槽位、记偏好。要跑模型的（生成建议）一律走
# `sends` 按钮发一句话，复用整条异步回合链路——卡片操作不该等十几秒。


# ---- 理解度探查的卡片操作（E 模块）-----------------------------------------


def _probe_card_from(record, *, step: str, note: str = "", verdict: dict | None = None) -> dict:
    """按记录重建探查卡。

    题干与选项都从库里取：用户答到一半关掉页面再回来，看到的是**同一道题**，
    而不是换一道新的、把他上面的作答作废。
    """
    return comprehension_probe_card(
        key="probe", probe_id=record.id, step=step,
        topic=record.topic, question=record.question,
        options=record.options or [], verdict=verdict, note=note,
    )


def _set_conversation_state(session, *, user_id: str, conversation_id: str, state: str) -> None:
    try:
        conversation = ConversationRepository(session, user_id=user_id).get(conversation_id)
    except PermissionError:
        return
    if conversation is not None:
        conversation.state = state
        session.commit()


def _swap_probe_card(session, *, user_id, conversation_id, card, new_card) -> None:
    _replace_card(
        session, user_id=user_id, conversation_id=conversation_id,
        card_key=str(card.get("key") or "probe"), card=new_card,
    )
    session.commit()


def _probe_set_level(session, *, user_id, conversation_id, card, level):
    """第 1 步：记下接触史。自评"第一次接触"当场定结论（guide），不问复述。"""
    from app.agent import probe

    try:
        record = probe.set_self_level(
            session, user_id=user_id,
            probe_id=str(card.get("probe_id") or ""), self_level=level,
        )
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    if record is None:
        raise ActionError("这道题已经不在了，重新点一次诊断")

    if record.verdict:
        new_card = _probe_card_from(
            record, step="verdict", verdict=probe.verdict_summary(record)
        )
        _swap_probe_card(session, user_id=user_id, conversation_id=conversation_id, card=card, new_card=new_card)
        return "好，那先读，不用诊断。", new_card

    new_card = _probe_card_from(record, step="judge", note="选一个最接近原文意思的说法。")
    _swap_probe_card(session, user_id=user_id, conversation_id=conversation_id, card=card, new_card=new_card)
    return "再看一道小题。", new_card


def _probe_answer(session, *, user_id, conversation_id, card, option_key):
    """第 2 步：判断题。答错且自称读过/能讲时，转入复述。"""
    from app.agent import probe

    try:
        record = probe.answer_judge(
            session, user_id=user_id,
            probe_id=str(card.get("probe_id") or ""), option_key=option_key,
        )
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    if record is None:
        raise ActionError("这道题已经不在了，重新点一次诊断")

    if probe.needs_recall(record.self_level, record.judge_correct):
        new_card = _probe_card_from(
            record, step="recall",
            note="用一句话说说它是什么——说错也没关系，我要看的是你抓住了哪几点。",
        )
        # 标记"等复述"：接下来用户说的话会被当成复述，而不是普通聊天
        _set_conversation_state(
            session, user_id=user_id, conversation_id=conversation_id, state="probing"
        )
        _swap_probe_card(session, user_id=user_id, conversation_id=conversation_id, card=card, new_card=new_card)
        return "再用一句话说说它是什么。", new_card

    new_card = _probe_card_from(record, step="verdict", verdict=probe.verdict_summary(record))
    _swap_probe_card(session, user_id=user_id, conversation_id=conversation_id, card=card, new_card=new_card)
    return probe.VERDICT_LABELS.get(record.verdict, "记下了。"), new_card


def probe_verdict_card(record) -> dict:
    """探查结论卡。给 copilot 在"复述作答之后"用——复述靠用户说话，
    这条路走的是对话入口，不是卡片操作。"""
    from app.agent import probe

    return _probe_card_from(record, step="verdict", verdict=probe.verdict_summary(record))


def _load_ctx(session: Session, *, user_id: str, conversation_id: str):
    from app.agent import intake

    conversation = ConversationRepository(session, user_id=user_id).get(conversation_id)
    if conversation is None:
        raise ActionError("会话已不存在，刷新页面看看")
    ctx = intake.load(session, conversation=conversation, user_id=user_id)
    return conversation, ctx


def intake_card(ctx, *, note: str = "", with_generate: bool = False) -> dict:
    from app.agent import intake

    # 发卡即进入"等作答"状态（调用方负责随后 save）
    intake.mark_card_sent(ctx)
    card = reading_intake_card(
        key="intake",
        round=ctx.round,
        inferred={"book": ctx.book, "basis": "system"},
        questions=intake.next_questions(ctx),
        digest_payload=intake.digest(ctx),
        note=note,
    )
    if with_generate:
        card["sends"] = [{"label": "按这些给建议", "message": intake_generate_phrase()}]
    return card


def intake_generate_phrase() -> str:
    """「生成建议」按钮发出去的那句话。

    必须被 router 的本地规则命中（`_SUGGEST_WORDS` 含「给我建议」），
    否则点一下会退化成一次模型分流，结果还不确定——这是本项目对
    `CAPABILITY_CATALOG.example` 的同一条要求。
    """
    return "确认，给我建议"


def _apply_intake_answer(session, *, user_id, conversation_id, card, slot, value):
    from app.agent import intake

    conversation, ctx = _load_ctx(session, user_id=user_id, conversation_id=conversation_id)
    try:
        intake.apply_answer(ctx, slot, value)
    except ValueError as exc:
        raise ActionError(str(exc)) from exc
    _record_intake(session, user_id=user_id, slot=slot, skipped=False)

    if ctx.is_ready:
        card = intake_card(
            ctx, note="确认没问题就生成；有不对的继续说。", with_generate=True
        )
        intake.save(session, conversation=conversation, ctx=ctx)
        session.commit()
        return "信息齐了，点下面生成建议。", card

    card = intake_card(ctx)
    intake.save(session, conversation=conversation, ctx=ctx)
    session.commit()
    return intake.confirm_text(ctx), card


def _apply_intake_skip(session, *, user_id, conversation_id, card):
    from app.agent import intake

    conversation, ctx = _load_ctx(session, user_id=user_id, conversation_id=conversation_id)
    intake.mark_skip(ctx)
    _record_intake(session, user_id=user_id, slot="", skipped=True)

    if ctx.is_ready:
        card = intake_card(ctx, note="确认没问题就生成。", with_generate=True)
        intake.save(session, conversation=conversation, ctx=ctx)
        session.commit()
        return "行，那就先这样。", card

    note = (
        "一直跳过也没关系，我只问最关键的那一句。"
        if ctx.reduced_mode
        else "不想答就跳过，按已知信息来。"
    )
    card = intake_card(ctx, note=note)
    intake.save(session, conversation=conversation, ctx=ctx)
    session.commit()
    return "跳过这轮。", card


def _record_intake(session, *, user_id, slot, skipped):
    try:
        from app.feedback import events

        events.record(
            session, user_id=user_id,
            event_type=events.AGENT_INTAKE_SKIPPED if skipped else events.AGENT_INTAKE_ANSWERED,
            payload={"slot": slot},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("intake 埋点失败: %s", exc)


def _reject_suggestion(session, *, user_id, card, item_id):
    """用户否掉一条建议。

    **不追问原因**——「这条不对」已经表达了足够信息；弹一个「为什么不对」的输入框，
    是把系统的困惑变成用户的负担。只记下组合（帮助类型 + 目标），供收敛用。
    """
    items = [it for it in (card.get("items") or []) if it.get("id") != item_id]
    digest = card.get("digest") or {}
    try:
        from app.feedback import events

        events.record(
            session, user_id=user_id, event_type=events.SUGGESTION_REJECTED,
            payload={
                "item_id": item_id,
                "help_type": digest.get("help_type") or [],
                "goal": digest.get("goal") or "",
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("建议拒绝埋点失败: %s", exc)

    new_card = dict(card)
    new_card["items"] = items
    if items:
        return "记下了，这条不再推。", new_card
    new_card["state"] = "insufficient_evidence"
    new_card["note"] = "这条不推了。要不要换个方向问？"
    return "记下了。", new_card


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
    # 不传 gateway：采纳/拒绝只改状态（必要时挂一条计划任务），不调模型——
    # 传进去只会白白构造一个 provider。
    ok, message = L5Orchestrator(session=session).decide(
        user_id=user_id, diagnosis_id=diagnosis_id, accepted=accepted
    )
    if not ok:
        raise ActionError(message or "这条诊断已经不在了")
    return message


def _keep_adjustment(session: Session, *, user_id: str, plan_id: str) -> str:
    """「保持原计划」——只记偏好，不重排、不调模型，所以是快操作。"""
    ok, message = L4Orchestrator(session=session).decide_adjustment(
        user_id=user_id, plan_id=plan_id, accepted=False
    )
    if not ok:
        raise ActionError(message or "计划不存在")
    return message
