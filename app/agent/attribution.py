"""冲突归因（D 模块）：把「两本书说法对立」解释成「为什么对立、各自何时成立」。

## 为什么不能只扩 `ConflictJudgment`

L2 的判定 schema 是「关系 + 类型 + 细节 + 建议」，它是**判定**任务：一句话够不够
撑起一处矛盾。归因是另一个任务：要读出作者的立场、前提、边界，还要给出可验证判据。
把两块塞进同一个 schema 会同时拉低两件事的精度（判定变慢变含糊、归因被挤成一句
"建议你对谈一下"）。所以独立调用、独立 schema、独立 task_type。

## 为什么七项里「背景」是必填

只说「前提不同」等于没解释：用户会问"那为什么前提不同"。背景（作者立场、时代、
学科范式、写作目的）恰恰回答了这个问题——它把"两个都对"变成可理解的事实。
这条在实践中也是最容易被模型交白卷的地方，所以校验写在代码里，不靠提示词自觉。

## 为什么惰性触发

归因一次要 reasoning=on，成本比判定高一个量级。全量回填历史冲突等于把过去所有
扫描的花费再花一遍，而绝大多数冲突用户根本不会点开。
所以：**用户点开才跑**，跑过就记 `attributed_at`，重复点开不再花钱。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.gateway import ModelGateway
from app.llm.prompts import CONFLICT_ATTRIBUTE
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

# 六类原因。分类的作用是让用户一眼看到"这是定义之争还是边界之争"——
# 前者要澄清用词，后者要划范围，处理方式完全不同。
CAUSE_TYPES = ("前提假设", "概念定义", "适用边界", "证据来源", "样本范围", "时效失效")
SCENE_FOLLOW = ("a", "b", "both")

# 低于这个置信度的冲突不归因：连"是不是真矛盾"都不确定，解释只会放大误判。
MIN_CONFIDENCE = 0.6
# 背景描述的最短长度（字）。太短的必然是套话（"背景不同"），视为未填写。
MIN_BACKGROUND_LEN = 8
# 背景的最长长度（字）。超过它基本可以断定模型在写分析报告，而不是一句判断——
# 真机产出出现过 150+ 字的背景，前半段全是"题面出处为…未提供作者信息…"，
# 用户要读三行才看到有用的那半句。
MAX_BACKGROUND_LEN = 120
# 每侧原文片段上限：归因需要前提与上下文，但不需要读完整本书
EXCERPT_MAX = 800


class SceneGuideItem(BaseModel):
    scene: str = Field(default="", max_length=200)
    follow: str = Field(default="both")
    reason: str = Field(default="", max_length=300)


class ProbeDraft(BaseModel):
    question: str = Field(default="", max_length=200)
    how_to_check: str = Field(default="", max_length=300)


class AttributionDraft(BaseModel):
    """归因输出。字段顺序与提示词一致，便于对照。"""

    cause_type: str = Field(default="", max_length=32)
    background_a: str = Field(default="", max_length=400)
    background_b: str = Field(default="", max_length=400)
    premise_a: str = Field(default="", max_length=400)
    premise_b: str = Field(default="", max_length=400)
    boundary_a: str = Field(default="", max_length=400)
    boundary_b: str = Field(default="", max_length=400)
    scene_guidance: list[SceneGuideItem] = Field(default_factory=list)
    probe: ProbeDraft = Field(default_factory=ProbeDraft)


@dataclass
class AttributionOutcome:
    state: str  # ok | already | skipped | degraded | not_found
    conflict_id: str = ""
    attribution: dict = field(default_factory=dict)
    scene_guidance: list = field(default_factory=list)
    probe: dict = field(default_factory=dict)
    note: str = ""


def is_attributed(conflict) -> bool:
    return bool(getattr(conflict, "attributed_at", None))


def _excerpt(text: str, *, limit: int = EXCERPT_MAX) -> str:
    body = (text or "").strip().replace("\n", " ")
    return body[:limit] + ("…" if len(body) > limit else "")


def _book_side_block(
    session: Session, *, user_id: str, suffix: str, claim, source_id: str
) -> str | None:
    """「书籍通读笔记」这一侧的文本。

    为什么单独处理：这类冲突的 `item_a_id` 是 `book:<id>` 影子来源，不是
    `knowledge_items` 里的行（见 `l2_orchestrator.is_book_source`）。
    拿它去查条目表必然查不到，原来的实现会把整处冲突判成"原文缺失"而跳过——
    也就是说所有「书 vs 笔记」的冲突永远得不到归因，用户看到的原因还是错的。
    """
    from app.domain.models.book import Book
    from app.domain.repositories.book_agent_reading_repository import BookAgentReadingRepository

    book_id = source_id[len("book:"):]
    book = session.get(Book, book_id)
    if book is None or getattr(book, "is_deleted", False):
        return None

    try:
        reading = BookAgentReadingRepository(session, user_id=user_id).get_by_book(book_id)
    except Exception:  # pragma: no cover - 读不到通读笔记就退化到主张
        reading = None

    statement = (claim.statement or "").strip() if claim is not None else ""
    if not statement and reading is not None:
        statement = (reading.summary or "").strip()[:300]
    if not statement:
        return None

    points: list[str] = []
    for chapter in (reading.chapters_note or [])[:6] if reading is not None else []:
        gist = str(chapter.get("gist") or "").strip()
        if gist:
            points.append(f"{chapter.get('title') or ''}：{gist}")
    excerpt = "；".join(points) or statement

    return (
        f"说法{suffix}（id={suffix}）\n"
        f"出处：《{book.title}》通读笔记（智能体读的，不是用户摘录）\n"
        f"主张：{statement}\n"
        f"原文片段：{_excerpt(excerpt)}\n"
    )


def _pair_block(session: Session, *, user_id: str, conflict) -> str | None:
    """拼双方说法 + 出处 + 原文片段。缺任何一侧都归因不了。"""
    from app.agent.l2_orchestrator import is_book_source

    claims = ClaimRepository(session, user_id=user_id)
    items = KnowledgeRepository(session, user_id=user_id)

    sides: list[str] = []
    for suffix, claim_id, item_id in (
        ("A", conflict.claim_a_id, conflict.item_a_id),
        ("B", conflict.claim_b_id, conflict.item_b_id),
    ):
        if not item_id:
            return None
        claim = claims.get(claim_id) if claim_id else None

        if is_book_source(item_id):
            block = _book_side_block(
                session, user_id=user_id, suffix=suffix, claim=claim, source_id=item_id
            )
        else:
            item = items.get(item_id)
            if item is None or claim is None:
                return None
            block = (
                f"说法{suffix}（id={suffix}）\n"
                f"出处：《{item.title}》\n"
                f"主张：{claim.statement}\n"
                f"原文片段：{_excerpt(item.raw_content or '')}\n"
            )

        if block is None:
            return None
        sides.append(block)
    return "\n".join(sides)


def _validate(draft: AttributionDraft) -> str:
    """校验七项。返回空串表示合格，否则返回不合格原因。

    为什么校验放代码不放提示词：这些条件都是**可判定**的（长度、枚举、条数），
    交给模型自觉遵守，等于把可判定的东西变成概率问题。
    """
    if draft.cause_type not in CAUSE_TYPES:
        return f"原因类型不在枚举内：{draft.cause_type!r}"
    for side, value in (("A", draft.background_a), ("B", draft.background_b)):
        text = (value or "").strip()
        if len(text) < MIN_BACKGROUND_LEN:
            return f"说法{side} 的背景缺失或过于笼统（背景是必填项）"
        if len(text) > MAX_BACKGROUND_LEN:
            return (
                f"说法{side} 的背景 {len(text)} 字，写成了一段分析而不是一句话——"
                f"只保留「作者站在什么立场、所以这样讲」那一句"
            )
    if not (draft.premise_a.strip() and draft.premise_b.strip()):
        return "双方前提至少各写一条"
    if not (draft.boundary_a.strip() and draft.boundary_b.strip()):
        return "双方适用边界至少各写一条"
    guides = [g for g in draft.scene_guidance if g.scene.strip() and g.reason.strip()]
    if len(guides) < 2:
        return "场景指引至少 2 条（什么情况下听谁）"
    for guide in guides:
        if guide.follow not in SCENE_FOLLOW:
            return f"场景指引的 follow 只能是 a/b/both，收到 {guide.follow!r}"
    if not (draft.probe.question.strip() and draft.probe.how_to_check.strip()):
        return "可验证判据缺失（用户自己怎么验）"
    return ""


def attribute_conflict(
    session: Session,
    *,
    user_id: str,
    conflict_id: str,
    gateway: ModelGateway | None = None,
    force: bool = False,
    context: dict | None = None,
) -> AttributionOutcome:
    """给一处冲突做归因。

    `context` 供测试注入（`{"paper": ...}`）时不调模型；生产传 None 走网关。
    """
    repo = ConflictRepository(session, user_id=user_id)
    try:
        conflict = repo.get(conflict_id)
    except PermissionError:
        return AttributionOutcome(state="not_found", conflict_id=conflict_id, note="无权访问这处冲突")
    if conflict is None:
        return AttributionOutcome(state="not_found", conflict_id=conflict_id, note="这处冲突已经不在了")

    if is_attributed(conflict) and not force:
        return AttributionOutcome(
            state="already",
            conflict_id=conflict_id,
            attribution=dict(conflict.attribution or {}),
            scene_guidance=list(conflict.scene_guidance or []),
            probe=dict(conflict.probe or {}),
            note="这处冲突已经分析过了",
        )

    if float(conflict.confidence or 0.0) < MIN_CONFIDENCE:
        return AttributionOutcome(
            state="skipped",
            conflict_id=conflict_id,
            note="这处冲突的置信度太低，先不做归因（解释一处可能并不存在的矛盾没有意义）",
        )

    pair = _pair_block(session, user_id=user_id, conflict=conflict)
    if pair is None:
        return AttributionOutcome(
            state="skipped", conflict_id=conflict_id, note="双方主张或原文缺失，无法归因"
        )

    injected = (context or {}).get("draft")
    if injected is not None:
        draft = injected
    else:
        gw = gateway or ModelGateway()
        prompt = (
            f"冲突类型（判定阶段给的）：{conflict.conflict_type or '未细分'}\n"
            f"判定阶段的描述：{conflict.detail or '（无）'}\n\n"
            f"{pair}"
        )
        try:
            completion = gw.chat(
                task_type="conflict_attribution",
                messages=[
                    {"role": "system", "content": CONFLICT_ATTRIBUTE.text},
                    {"role": "user", "content": prompt},
                ],
                user_id=user_id,
                session=session,
                prompt_version=CONFLICT_ATTRIBUTE.version,
                json_model=AttributionDraft,
            )
            draft = parse_structured(
                completion.text, validator=lambda d: AttributionDraft(**d)
            )
        except JsonParseError as exc:
            logger.warning("attribution: 输出无法解析 conflict=%s: %s", conflict_id, exc)
            return AttributionOutcome(
                state="degraded", conflict_id=conflict_id, note="这次没分析出来，重试一下？"
            )
        except Exception as exc:  # noqa: BLE001 - 模型层异常不该把请求炸掉
            logger.warning("attribution: 调用失败 conflict=%s: %s", conflict_id, exc)
            return AttributionOutcome(
                state="degraded", conflict_id=conflict_id, note="这次没分析出来，重试一下？"
            )

    problem = _validate(draft)
    if problem:
        logger.info("attribution: 不合格 conflict=%s reason=%s", conflict_id, problem)
        return AttributionOutcome(
            state="degraded", conflict_id=conflict_id, note=f"归因不完整，未保存：{problem}"
        )

    attribution = {
        "cause_type": draft.cause_type,
        "background_a": draft.background_a.strip(),
        "background_b": draft.background_b.strip(),
        "premise_a": draft.premise_a.strip(),
        "premise_b": draft.premise_b.strip(),
        "boundary_a": draft.boundary_a.strip(),
        "boundary_b": draft.boundary_b.strip(),
    }
    guidance = [
        {"scene": g.scene.strip(), "follow": g.follow, "reason": g.reason.strip()}
        for g in draft.scene_guidance
        if g.scene.strip() and g.reason.strip()
    ]
    probe = {
        "question": draft.probe.question.strip(),
        "how_to_check": draft.probe.how_to_check.strip(),
    }

    repo.set_attribution(
        conflict, attribution=attribution, scene_guidance=guidance, probe=probe
    )
    session.commit()

    try:
        from app.feedback import events

        events.record(
            session, user_id=user_id, event_type=events.CONFLICT_ATTRIBUTED,
            payload={
                "conflict_id": conflict_id,
                "cause_type": attribution["cause_type"],
                "scenes": len(guidance),
            },
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("attribution: 埋点失败: %s", exc)

    return AttributionOutcome(
        state="ok",
        conflict_id=conflict_id,
        attribution=attribution,
        scene_guidance=guidance,
        probe=probe,
        note="已分析",
    )
