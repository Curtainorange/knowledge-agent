"""阅读建议生成（B 模块）：确认过的诉求 + 已同步依据 → 1–2 条可执行建议。

这一层只做三件事，且**顺序不能变**：

1. **判定**：槽位不齐 → 不出建议（交回确认）；依据不够 → 不出建议（说明缺什么）。
2. **生成**：把上下文与依据片段交给模型，只要结构化输出（带 `basis_item_ids`）。
3. **校验**：引用的 id 必须真在这次的证据池里；不在就整条丢弃。

## 为什么校验必须在代码里

提示词写了「不许编造 id」，但提示词约束不了幻觉——模型完全可能给出一个格式正确、
id 却不存在的建议。那种建议最危险：它看起来有出处，用户点开却什么都没有。
所以最终判定权在 `grounding.verify_citations`：引用不实的建议整条丢弃，
**不保留文案只删 id**（文案本身就是由那条不存在依据撑起来的，删 id 等于把幻觉的
出处抹掉、把幻觉留下）。

## 为什么不在这里拼文案

自然语言回复由 `cards.suggestion_card` 与调用方生成，本模块只产出结构化结果。
两个地方各写一份文案，迟早会出现"卡片说有依据、回复说没找到"这种自相矛盾。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import grounding, intake
from app.llm.gateway import ModelGateway
from app.llm.prompts import SUGGESTION
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

# 每条建议最多引用几条证据：再多用户不会看，也让卡片失去重点
MAX_BASIS_PER_ITEM = 5
# 证据池上限：喂给模型的片段不是越多越好，噪音会稀释相关性
MAX_EVIDENCE = 8
# 用户否过某类建议后的收敛窗口（天）。与验收清单第 1 条对应用：
# 「不强制」不只是措辞客气，而是**被否掉之后真的改变行为**。
RESTRAIN_DAYS = 7


class SuggestionDraftItem(BaseModel):
    """单条建议。四要素缺一，这条就不合格。"""

    action: str = Field(default="", max_length=300)
    why: str = Field(default="", max_length=500)
    steps: list[str] = Field(default_factory=list)
    how_to_verify: str = Field(default="", max_length=300)
    basis_item_ids: list[str] = Field(default_factory=list)


class SuggestionDraft(BaseModel):
    items: list[SuggestionDraftItem] = Field(default_factory=list)
    reason: str = Field(default="", max_length=300)  # 依据不足时说明缺什么


@dataclass
class SuggestionOutcome:
    """生成结果。`state` 决定调用方发哪种卡。"""

    state: str  # ok | incomplete | insufficient_evidence | degraded
    digest: dict = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)
    evidence: list = field(default_factory=list)
    references: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    note: str = ""
    dropped: int = 0
    hints: list[str] = field(default_factory=list)


def _context_block(ctx: intake.IntakeContext) -> str:
    """把用户确认过的上下文写成"事实块"。模型只许照这个来，不许自己猜。"""
    book = ctx.book or {}
    lines = ["用户上下文（已确认，视为事实）："]
    if book.get("title"):
        where = f"，读到{book['chapter']}" if book.get("chapter") else ""
        progress = book.get("progress")
        if isinstance(progress, (int, float)) and 0 < progress < 1:
            where += f"（约 {int(progress * 100)}%）"
        lines.append(f"- 正在读：《{book['title']}》{where}")
    goal_label = intake._label_of(intake.GOAL_OPTIONS, ctx.goal) or ctx.goal_note or "未说明"
    lines.append(f"- 阅读目标：{goal_label}")
    level_label = intake._label_of(intake.SELF_LEVEL_OPTIONS, ctx.self_level) or "未说明"
    lines.append(f"- 理解程度：{level_label}")
    if ctx.question.strip():
        lines.append(f"- 想解决的问题：{ctx.question.strip()}")
    if ctx.help_type:
        labels = "、".join(
            intake._label_of(intake.HELP_TYPE_OPTIONS, h) or h for h in ctx.help_type
        )
        lines.append(f"- 希望的帮助方式：{labels}")
    return "\n".join(lines)


def _evidence_block(evidence: list) -> str:
    """证据片段块。id 必须与 `Evidence.evidence_id` 完全一致，否则校验会整条丢弃。"""
    lines = ["依据片段（只能引用这些，id 在方括号里）："]
    for ev in evidence:
        lines.append(f"[{ev.evidence_id}] {ev.title}：「{ev.quote}」")
    return "\n".join(lines)


def build_query(ctx: intake.IntakeContext) -> str:
    """召回用的 query：以具体疑问为主，目标描述补语义。

    只用 question 会漏掉"目标"里真正关键的词（比如"用到团队管理上"可能写在 goal_note 里）；
    只用 goal 又太泛。两个拼起来最稳。
    """
    parts = [ctx.question.strip(), ctx.goal_note.strip()]
    book = ctx.book or {}
    if book.get("title"):
        parts.append(str(book["title"]))
    return " ".join(p for p in parts if p).strip()


def _restrained_reason(session: Session, *, user_id: str, ctx: intake.IntakeContext) -> str:
    """最近是否否过同类建议（同目标 + 有交集的帮助类型）。

    读失败一律按"没否过"处理：收敛是体验优化，不该因为读不到事件就不给建议。
    """
    try:
        from datetime import datetime, timedelta, timezone

        from app.domain.repositories.learning_event_repository import LearningEventRepository
        from app.feedback import events

        rows = LearningEventRepository(session, user_id=user_id).list_recent(
            user_id, event_type=events.SUGGESTION_REJECTED, limit=20
        )
    except Exception:  # pragma: no cover - 防御性兜底
        return ""

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=RESTRAIN_DAYS)
    wanted = set(ctx.help_type or [])
    for row in rows:
        occurred = getattr(row, "occurred_at", None)
        if occurred is not None and occurred < cutoff:
            continue
        payload = getattr(row, "payload", None) or {}
        if str(payload.get("goal") or "") != (ctx.goal or ""):
            continue
        if wanted & set(payload.get("help_type") or []):
            return "这类建议你之前否过，我们先换个方向——你可以说说哪里不对，或者换个问法。"
    return ""


def generate(
    session: Session,
    *,
    user_id: str,
    ctx: intake.IntakeContext,
    gateway: ModelGateway | None = None,
    embedding=None,
) -> SuggestionOutcome:
    """生成建议。返回的 `state` 决定调用方怎么呈现。"""
    digest = intake.digest(ctx)

    # 门禁：确认没完成就别生成。这不是提示词能拦的事——绕过模型直接判定。
    if not ctx.is_ready:
        return SuggestionOutcome(
            state="incomplete",
            digest=digest,
            note="先把这几项确认一下，我才能给到对你有用的建议。",
        )

    query = build_query(ctx)
    restrained = _restrained_reason(session, user_id=user_id, ctx=ctx)
    if restrained:
        return SuggestionOutcome(
            state="restrained",
            digest=digest,
            note=restrained,
            hints=[
                "换个角度描述你的问题",
                "说说上次那条哪里不对",
                "先记一条你觉得对的说法",
            ],
        )

    evidence = grounding.build_evidence(
        session, user_id=user_id, query=query, top_k=MAX_EVIDENCE, embedding=embedding
    )
    evidence = grounding.dedupe_by_book(evidence)
    citable = [ev for ev in evidence if ev.can_cite]

    if not citable:
        return SuggestionOutcome(
            state="insufficient_evidence",
            digest=digest,
            evidence=evidence,
            note="你库里没有能支撑这个问题相关内容，我不猜。",
            hints=[
                "换个说法，用你笔记里出现过的词",
                "先把这段想法记一条，再来问",
                "去对应章节划一段存进来",
            ],
        )

    gw = gateway or ModelGateway()
    prompt = f"{_context_block(ctx)}\n\n{_evidence_block(citable)}"
    try:
        completion = gw.chat(
            task_type="reading_suggestion",
            messages=[
                {"role": "system", "content": SUGGESTION.text},
                {"role": "user", "content": prompt},
            ],
            user_id=user_id,
            session=session,
            prompt_version=SUGGESTION.version,
            json_model=SuggestionDraft,
        )
        draft = parse_structured(completion.text, validator=lambda d: SuggestionDraft(**d))
    except JsonParseError as exc:
        logger.warning("suggestion: 模型输出无法解析: %s", exc)
        return SuggestionOutcome(
            state="degraded", digest=digest, evidence=citable,
            note="这次没生成成功，重试一下？",
        )
    except Exception as exc:  # noqa: BLE001 - 模型层任何异常都不该把回合炸掉
        logger.warning("suggestion: 调用失败: %s", exc)
        return SuggestionOutcome(
            state="degraded", digest=digest, evidence=citable,
            note="这次没生成成功，重试一下？",
        )

    raw_items = [
        {
            "id": f"sg{i}",
            "action": it.action.strip(),
            "why": it.why.strip(),
            "steps": [s.strip() for s in it.steps if s and s.strip()][:4],
            "how_to_verify": it.how_to_verify.strip(),
            "basis_item_ids": [b.strip() for b in it.basis_item_ids if b and b.strip()][
                :MAX_BASIS_PER_ITEM
            ],
        }
        for i, it in enumerate(draft.items, start=1)
    ]

    kept, dropped = grounding.verify_citations(raw_items, citable)
    # 四要素缺失也丢弃：缺 how_to_verify 的建议用户没法判断有没有用
    complete = [
        it for it in kept
        if it["action"] and it["why"] and it["how_to_verify"] and it["basis_item_ids"]
    ]
    dropped += len(kept) - len(complete)

    # 最多两条：三条以上就变成"清单"，用户只会挑顺眼的那条看
    complete = complete[:2]

    if not complete:
        return SuggestionOutcome(
            state="insufficient_evidence",
            digest=digest,
            evidence=citable,
            dropped=dropped,
            note=(
                "我找到的依据不足以支撑一条可执行建议，所以先不给。"
                if not draft.reason else f"{draft.reason}"
            ),
            hints=[
                "换个说法，用你笔记里出现过的词",
                "先把这段想法记一条，再来问",
            ],
        )

    # 整体置信度取"最弱的那条"：一条建议由 3 条依据、另一条只有 1 条，
    # 整体可信度由 1 条那条决定，不该被平均抬高。
    weakest = min(len(it["basis_item_ids"]) for it in complete)
    used_ids = {b for it in complete for b in it["basis_item_ids"]}
    rels = [ev.relevance for ev in citable if ev.evidence_id in used_ids]
    # 槽位"完整"= 用户确实回答了目标 / 水平 / 帮助类型，而不是跳过后拿默认值。
    # 跳过的建议可以给，但不该显示得和"确认过的建议"一样可靠。
    slots_complete = not (ctx.skipped or ctx.reduced_mode)
    confidence = grounding.confidence_for(
        basis_count=weakest,
        mean_relevance=(sum(rels) / len(rels)) if rels else 0.0,
        slots_complete=slots_complete,
        dropped=dropped,
    )
    for it in complete:
        it["confidence"] = confidence

    outcome = SuggestionOutcome(
        state="ok",
        digest=digest,
        items=complete,
        evidence=citable,
        confidence=confidence,
        dropped=dropped,
    )
    if dropped:
        outcome.note = f"另外 {dropped} 条建议因依据不足已省略。"

    _record(session, user_id=user_id, outcome=outcome)
    return outcome


def _record(session: Session, *, user_id: str, outcome: SuggestionOutcome) -> None:
    """埋点：只记元数据（条数 / 依据数 / 置信度），不记建议原文。

    原文进事件表会让行为日志变成内容仓库，且用户删了条目后日志里还留着正文。
    """
    try:
        from app.feedback import events

        events.record(
            session, user_id=user_id, event_type=events.SUGGESTION_CREATED,
            payload={
                "items": len(outcome.items),
                "evidence": len(outcome.evidence),
                "confidence": outcome.confidence,
                "dropped": outcome.dropped,
            },
        )
    except Exception as exc:  # pragma: no cover - 埋点失败不影响主链路
        logger.warning("suggestion: 埋点失败: %s", exc)


def reply_text(outcome: SuggestionOutcome) -> str:
    """配套回复的一句话。与卡片不得互相矛盾（见模块 docstring）。"""
    if outcome.state == "incomplete":
        return outcome.note or "先确认几项信息。"
    if outcome.state == "insufficient_evidence":
        return outcome.note or "我找到的依据不足以给建议。"
    if outcome.state == "degraded":
        return outcome.note or "这次没生成成功。"
    if len(outcome.items) == 1:
        return "按你确认的目标，我想到一条可以试的。"
    return f"按你确认的目标，我想到 {len(outcome.items)} 条可以试的。"
