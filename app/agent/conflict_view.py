"""冲突展示视图与「未解冲突」共享查询——L2 产物流向各消费方的唯一入口。

`conflict_views` 原住在 `turns` 里。但 L3（认知简报）、L5（诊断归因）、L4（计划上下文）
与 greeting（开场）都要读冲突内容，而 `turns` **反向**导入前三个 orchestrator——
直接复用就会形成循环导入。所以把它下沉到本模块：既是打破环的手段，
也让「一条冲突长什么样」只有一份口径。

`top_unresolved` 是「最该处理的那一处」的唯一定义：未处理（unseen）里按置信度取前 N。
开场用它直击要点、简报与诊断用它当推理原料、L4 用它判断是否与目标相关——
这三处若各写各的排序规则，就会出现「开场说的是 A、简报问的是 B」这种静默分叉。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConflictBrief:
    """一条冲突的要点（不含用户处理状态与建议）。

    `claim_a_id` / `claim_b_id` 保留下来，是为了让下游能直接取这两条主张已落库的
    向量（`Claim.embedding`）做语义相关判断——既不必重新 embedding，
    也天然与 L2 落库时的文本口径一致（口径不一致算出来的余弦没有意义）。
    """

    conflict_id: str
    title_a: str
    title_b: str
    claim_a: str
    claim_b: str
    claim_a_id: str = ""
    claim_b_id: str = ""
    conflict_type: str = ""
    confidence: float = 0.0


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
            "claim_a_id": row.claim_a_id or "",
            "claim_b_id": row.claim_b_id or "",
            "conflict_type": row.conflict_type,
            "detail": row.detail,
            "suggestion": row.suggestion,
            "confidence": row.confidence,
            "user_state": row.user_state,
        })
    return views


def top_unresolved(session: Session, *, user_id: str, limit: int = 3) -> list[ConflictBrief]:
    """未处理冲突里最该处理的几条：按置信度降序（置信度相同则较新的在前）。

    只看 `unseen`——用户已经处理过（采纳 / 忽略）的不该再出现在开场或诊断里。
    """
    from app.domain.repositories.conflict_repository import ConflictRepository

    rows = ConflictRepository(session, user_id=user_id).list_by_user(
        user_id, state="unseen", limit=max(limit * 5, 20)
    )
    if not rows:
        return []

    # list_by_user 已按 created_at 倒序；Python 的 sort 稳定，故「同置信度取较新的」
    ranked = sorted(rows, key=lambda r: -float(r.confidence or 0.0))
    picked = ranked[:limit]

    by_id = {
        v["conflict_id"]: v
        for v in conflict_views(session, user_id=user_id, conflict_ids=[r.id for r in picked])
    }
    briefs: list[ConflictBrief] = []
    for row in picked:
        view = by_id.get(row.id)
        if view is None:  # 条目被删/越权取不到就跳过，不让一条脏数据拖垮开场
            continue
        briefs.append(ConflictBrief(
            conflict_id=view["conflict_id"],
            title_a=view["title_a"],
            title_b=view["title_b"],
            claim_a=view["claim_a"],
            claim_b=view["claim_b"],
            claim_a_id=view.get("claim_a_id", ""),
            claim_b_id=view.get("claim_b_id", ""),
            conflict_type=view["conflict_type"] or "",
            confidence=float(view["confidence"] or 0.0),
        ))
    return briefs


def format_conflicts(briefs: list[ConflictBrief]) -> str:
    """拼成给模型读的多行文本（各消费方共用，避免措辞分叉）。"""
    return "\n".join(
        f"- 《{b.title_a}》主张「{b.claim_a}」，而《{b.title_b}》主张「{b.claim_b}」"
        + (f"（类型：{b.conflict_type}）" if b.conflict_type else "")
        for b in briefs
    )


def unresolved_section(session: Session, *, user_id: str, limit: int | None = None) -> str:
    """未解冲突的多行要点文本；无冲突或读取失败返回空串。

    调用方直接把它拼进 user message 即可（空串等于「没有这段」）。各能力
    （L3 简报 / L5 诊断 / L4 计划与归因）都从这里取，保证「取哪几条、怎么写」
    只有一份实现——三处各写各的，就会出现「简报问的是 A、诊断看的是 B」这种分叉。
    """
    from app.core.config import settings

    if limit is None:
        limit = settings.conflict_context_limit
    try:
        briefs = top_unresolved(session, user_id=user_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 读冲突失败不该阻断调用方的主任务
        logger.warning("读未解冲突失败，跳过该段: %s", exc)
        return ""
    return format_conflicts(briefs)
