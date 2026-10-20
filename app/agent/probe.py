"""理解度探查（E 模块）：先问再判，把「没摘录」和「没读懂」分开。

## 两轮探什么

| 轮 | 问什么 | 回答方式 | 用来区分 |
|---|---|---|---|
| 1 | 接触史（首次 / 读过 / 能讲）+ 一道判断题 | 卡片按钮 | 有没有基础 |
| 2 | "用一句话说说它是什么" | 自由输入 | 是不是真懂 |

第 2 轮**不是每人都问**：
- 自评「第一次接触」→ 不追问。判错是正常的，那是还没学，不是没学懂。
- 判断题答对 → 不追问。已经证明读懂了，再问复述是浪费时间。
- 只有「自称读过 / 能讲」**且**判断题答错 → 才追问复述。

## 判定矩阵（代码算，不交模型）

| 自评 | 判断题 | 复述覆盖率 | 结论 |
|---|---|---|---|
| 第一次接触 | — | — | guide（先读，不诊断） |
| 读过 / 能讲 | 对 | — | mastered（已掌握，只可能建议"整理成体系"） |
| 读过 / 能讲 | 错 | < 0.4 | gap（理解缺口，可出诊断） |
| 读过 / 能讲 | 错 | ≥ 0.4 | bias（方向对、表述偏，出"对齐说法"不出"你没懂"） |

## 为什么复述判分不调模型

出题时已经产出了 3 条 `key_points`（见 `PROBE_QUESTION` 提示词）。复述按
「2-gram 覆盖率」判分：可复现、可解释（能告诉用户漏了哪一条），而且省一次模型调用。
代价是它只认"说了相关的话"，不认"说得对"——但这正是我们要区分的层级：
说得出要点说明读过；说得对不对是下一层的事，不该在这一步判。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.comprehension_probe import ComprehensionProbe
from app.llm.gateway import ModelGateway
from app.llm.prompts import PROBE_QUESTION
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

VERDICT_GUIDE = "guide"
VERDICT_MASTERED = "mastered"
VERDICT_GAP = "gap"
VERDICT_BIAS = "bias"
VERDICT_UNKNOWN = "unknown"

VERDICT_LABELS = {
    VERDICT_GUIDE: "第一次接触，先读，不做诊断",
    VERDICT_MASTERED: "已经掌握，只可能建议整理成体系",
    VERDICT_GAP: "读的时候漏了关键内容",
    VERDICT_BIAS: "方向对，但表述和原文有偏差",
    VERDICT_UNKNOWN: "信息不足，暂不判断",
}

# 复述覆盖率的判定线：低于它算"没抓住核心"
COVERAGE_THRESHOLD = 0.4
# 单条要点被复述"覆盖"的判定线（2-gram 重叠比例）
POINT_HIT_THRESHOLD = 0.3
# 结论有效期：同一个主题在这段时间内不再重复问
VERDICT_TTL_DAYS = 7
# 判分用的片段长度上限
EXCERPT_MAX = 1200

SELF_LEVEL_VALUES = ("first", "once", "teach")

# 探查状态（供 preflight 判断该发哪张卡）
STATE_NEED_QUESTION = "need_question"
STATE_NEED_RECALL = "need_recall"
STATE_ALLOW = "allow"


class ProbeOption(BaseModel):
    key: str = Field(default="", max_length=8)
    text: str = Field(default="", max_length=200)
    correct: bool = False
    why: str = Field(default="", max_length=200)


class ProbeDraft(BaseModel):
    topic: str = Field(default="", max_length=128)
    question: str = Field(default="", max_length=300)
    options: list[ProbeOption] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)


@dataclass
class ProbeState:
    """当前该做什么。`existing` 是还没答完的那条记录（用于原样重发同一道题）。"""

    state: str
    verdict: str = ""
    record: ComprehensionProbe | None = None
    note: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _grams(text: str, n: int = 2) -> set[str]:
    clean = "".join(ch for ch in (text or "") if not ch.isspace())
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i:i + n] for i in range(len(clean) - n + 1)}


def coverage_of(recall_text: str, key_points: list[str]) -> tuple[list[str], list[str], float]:
    """复述覆盖了哪几条要点。返回 (covered, missed, coverage)。"""
    points = [str(p).strip() for p in (key_points or []) if str(p).strip()]
    if not points:
        return [], [], 0.0
    text = (recall_text or "").strip()
    if not text:
        return [], points, 0.0

    text_grams = _grams(text)
    covered: list[str] = []
    missed: list[str] = []
    for point in points:
        p_grams = _grams(point)
        if not p_grams:
            continue
        overlap = len(p_grams & text_grams) / len(p_grams)
        (covered if overlap >= POINT_HIT_THRESHOLD else missed).append(point)
    total = len(covered) + len(missed)
    return covered, missed, (len(covered) / total if total else 0.0)


def decide(*, self_level: str, judge_correct: bool | None, coverage: float) -> str:
    """判定矩阵。**纯函数**：同样的输入永远同样的结论，也才测得住。"""
    if self_level == "first":
        return VERDICT_GUIDE
    if judge_correct is None:
        return VERDICT_UNKNOWN
    if judge_correct:
        return VERDICT_MASTERED
    return VERDICT_GAP if coverage < COVERAGE_THRESHOLD else VERDICT_BIAS


def needs_recall(self_level: str, judge_correct: bool | None) -> bool:
    """要不要追问复述。只有「自称读过/能讲」且判断题答错时才追问。"""
    if self_level == "first":
        return False
    return judge_correct is False


# ---- 出题 -------------------------------------------------------------------


def _probe_source(session: Session, *, user_id: str) -> tuple[str, str, str] | None:
    """挑一段值得探查的内容：优先"正在读的书/条目"，其次最新条目。

    返回 (topic, item_id, excerpt)。都取不到返回 None（知识库空）。
    """
    from app.agent import intake
    from app.domain.models.knowledge_item import KnowledgeItem
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    target = intake.infer_reading_target(session, user_id=user_id)
    book = (target or {}).get("book") or {}

    repo = KnowledgeRepository(session, user_id=user_id)
    items = repo.list_active(user_id)
    if not items:
        return None

    book_title = str(book.get("title") or "").strip()
    if book_title:
        for item in items:
            if book_title in (item.title or ""):
                return item.title, item.id, (item.raw_content or "")[:EXCERPT_MAX]

    newest = items[-1] if items else None
    if newest is None:
        return None
    return newest.title or "", newest.id, (newest.raw_content or "")[:EXCERPT_MAX]


def build_probe(
    session: Session, *, user_id: str, gateway: ModelGateway | None = None, context: dict | None = None
) -> ProbeDraft | None:
    """生成一道判断题。取不到内容或输出不合格都返回 None（调用方降级：直接放行诊断）。"""
    injected = (context or {}).get("draft")
    if injected is not None:
        return injected

    source = _probe_source(session, user_id=user_id)
    if source is None:
        return None
    topic, _item_id, excerpt = source
    if not excerpt.strip():
        return None

    gw = gateway or ModelGateway()
    try:
        completion = gw.chat(
            task_type="probe_question",
            messages=[
                {"role": "system", "content": PROBE_QUESTION.text},
                {"role": "user", "content": f"标题：{topic}\n内容片段：{excerpt}"},
            ],
            user_id=user_id,
            session=session,
            prompt_version=PROBE_QUESTION.version,
            json_model=ProbeDraft,
        )
        draft = parse_structured(completion.text, validator=lambda d: ProbeDraft(**d))
    except JsonParseError as exc:
        logger.warning("probe: 出题输出无法解析: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 出题失败不该把诊断链路炸掉
        logger.warning("probe: 出题调用失败: %s", exc)
        return None

    if not _draft_ok(draft):
        logger.info("probe: 出题不合格，放弃本次探查")
        return None
    return draft


def _draft_ok(draft: ProbeDraft) -> bool:
    """题干、三个选项、恰好一个正确、三条要点——缺一这道题就没法用。"""
    if not draft.question.strip():
        return False
    options = [o for o in draft.options if o.text.strip()]
    if len(options) < 3:
        return False
    if sum(1 for o in options if o.correct) != 1:
        return False
    points = [p for p in draft.key_points if str(p).strip()]
    if len(points) < 3:
        return False
    # 选项 key 必须唯一且非空：前端与判分都靠它
    keys = [o.key for o in options]
    return len(set(keys)) == len(keys) and all(keys)


# ---- 持久化与状态 -----------------------------------------------------------


def save_question(
    session: Session,
    *,
    user_id: str,
    draft: ProbeDraft,
    topic: str = "",
    item_id: str | None = None,
) -> ComprehensionProbe:
    record = ComprehensionProbe(
        user_id=user_id,
        topic=(topic or draft.topic or "")[:128],
        item_id=item_id,
        round=1,
        question=draft.question.strip(),
        options=[
            {"key": o.key, "text": o.text, "correct": o.correct, "why": o.why}
            for o in draft.options
            if o.text.strip()
        ],
        key_points=[str(p).strip() for p in draft.key_points if str(p).strip()][:5],
    )
    session.add(record)
    session.commit()
    return record


def latest(session: Session, *, user_id: str, days: int = VERDICT_TTL_DAYS) -> ComprehensionProbe | None:
    cutoff = _now() - timedelta(days=days)
    try:
        return session.scalars(
            select(ComprehensionProbe)
            .where(
                ComprehensionProbe.user_id == user_id,
                ComprehensionProbe.created_at >= cutoff,
            )
            .order_by(ComprehensionProbe.created_at.desc())
            .limit(1)
        ).first()
    except Exception:  # pragma: no cover - 表不存在等极端情况按"没有"处理
        return None


def current_state(session: Session, *, user_id: str, days: int = VERDICT_TTL_DAYS) -> ProbeState:
    """该发哪张卡 / 该不该放行诊断。**preflight 的唯一判据**。"""
    record = latest(session, user_id=user_id, days=days)
    if record is None:
        return ProbeState(state=STATE_NEED_QUESTION)

    if record.verdict:
        if record.verdict == VERDICT_GUIDE:
            return ProbeState(state=STATE_ALLOW, verdict=record.verdict, record=record)
        return ProbeState(state=STATE_ALLOW, verdict=record.verdict, record=record)

    if not record.judge_key:
        # 出过题但没答：原样再摆一次，不换题
        return ProbeState(state=STATE_NEED_QUESTION, record=record)

    if record.round == 1 and needs_recall(record.self_level, record.judge_correct):
        return ProbeState(state=STATE_NEED_RECALL, record=record)

    return ProbeState(state=STATE_ALLOW, record=record)


def set_self_level(
    session: Session, *, user_id: str, probe_id: str, self_level: str
) -> ComprehensionProbe | None:
    """第 1 步：记下自评（接触史）。

    与判断题分开两次点击，是因为它们回答两个不同问题：先定"有没有基础"，
    再定"理解得对不对"。合成一屏两个问题，用户会以为在填表。
    """
    record = _get(session, user_id=user_id, probe_id=probe_id)
    if record is None:
        return None
    if self_level not in SELF_LEVEL_VALUES:
        raise ValueError(f"未知理解程度：{self_level!r}")
    record.self_level = self_level
    # 自评"第一次接触"的人不必再答题：先读，不诊断
    if self_level == "first":
        record.verdict = VERDICT_GUIDE
    session.commit()
    return record


def answer_judge(
    session: Session, *, user_id: str, probe_id: str, option_key: str
) -> ComprehensionProbe | None:
    """第 2 步：记录判断题选项，算出对错，并立刻定下不需要复述时的结论。"""
    record = _get(session, user_id=user_id, probe_id=probe_id)
    if record is None:
        return None
    if not record.self_level:
        raise ValueError("还没选择接触史")

    options = record.options or []
    chosen = next((o for o in options if str(o.get("key")) == option_key), None)
    if chosen is None:
        raise ValueError(f"没有这个选项：{option_key!r}")

    record.judge_key = option_key
    record.judge_correct = bool(chosen.get("correct"))
    session.commit()

    if not needs_recall(record.self_level, record.judge_correct):
        # 不需要复述：直接定结论（首次接触 → guide；答对 → mastered）
        record.verdict = decide(
            self_level=record.self_level,
            judge_correct=record.judge_correct,
            coverage=0.0,
        )
        session.commit()
    return record


def answer_recall(
    session: Session, *, user_id: str, probe_id: str, recall_text: str
) -> ComprehensionProbe | None:
    """第 2 轮作答：复述按要点覆盖率判分，然后定结论。"""
    record = _get(session, user_id=user_id, probe_id=probe_id)
    if record is None:
        return None

    covered, missed, coverage = coverage_of(recall_text, record.key_points or [])
    record.round = 2
    record.recall_text = (recall_text or "").strip()[:2000]
    record.covered = covered
    record.missed = missed
    record.coverage = round(coverage, 4)
    record.verdict = decide(
        self_level=record.self_level,
        judge_correct=record.judge_correct,
        coverage=coverage,
    )
    session.commit()
    return record


def _get(session: Session, *, user_id: str, probe_id: str) -> ComprehensionProbe | None:
    record = session.get(ComprehensionProbe, probe_id)
    if record is None or record.user_id != user_id:
        return None   # 越权与不存在同一处理：不泄漏存在性
    return record


def verdict_summary(record: ComprehensionProbe | None) -> dict:
    """给卡片与诊断用的结论摘要。"""
    if record is None:
        return {}
    return {
        "verdict": record.verdict,
        "verdict_label": VERDICT_LABELS.get(record.verdict, ""),
        "self_level": record.self_level,
        "judge_correct": record.judge_correct,
        "coverage": record.coverage,
        "covered": list(record.covered or []),
        "missed": list(record.missed or []),
    }
