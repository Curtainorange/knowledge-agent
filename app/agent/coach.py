"""主动学习教练（第二阶段 C4）：L3/L4/L5 建议的纯本地聚合 → 教练提示推送。

聚合本身**零 LLM**（照 `push_service.build_weekly_digest` 的纯本地拼装范式）：
把散在各处的「该做的事」——L5 待决定的诊断建议、L4 偏离信号、目标进度——
与 L3 的深度追问拼成一条教练提示。唯一 LLM 成本在调用方跑 L3 `brief()`。

设计要点：

- **事件只引元数据**：偏离信号取 `L4_DEVIATION_CHECKED` 事件 payload 里的
  reasons 标签（本地信号文案，属元数据），不搬运归因正文；
- **空材料不打扰**：没有任何可说的就返回 None，不发推送；
- **防鸡汤化**：每块必须带「下一步：」可执行动作，说不出下一步的内容不上推送。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import goal_tracking
from app.core.config import settings
from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.feedback import events


@dataclass
class CoachBlock:
    """一块教练提示：一个结论 + 一个可执行下一步。"""

    label: str      # 「待决定建议」/「偏离信号」/「目标进度」
    summary: str    # 结论一句
    next_step: str  # 下一步一句（必须非空，防鸡汤化）


@dataclass
class CoachMaterials:
    blocks: list[CoachBlock] = field(default_factory=list)
    eval_line: str = ""   # 弱真值基线附注（无数据则空）


@dataclass
class CoachDigest:
    week_label: str
    title: str
    body: str


def collect_materials(session: Session, user_id: str, now: datetime | None = None) -> CoachMaterials:
    """纯读收集教练素材（不触模型、不写库）。"""
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    week_start = now - timedelta(days=7)
    materials = CoachMaterials()

    # ① L5 待决定的诊断建议（≤2）：状态 pending = 还没被用户采纳/拒绝
    pending = list(session.scalars(
        select(CognitiveDiagnosis)
        .where(CognitiveDiagnosis.user_id == user_id, CognitiveDiagnosis.status == "pending")
        .order_by(CognitiveDiagnosis.created_at.desc())
        .limit(2)
    ))
    for diag in pending:
        materials.blocks.append(CoachBlock(
            label="待决定建议",
            summary=f"{diag.pattern}：{diag.suggested_action}",
            next_step="在通知页决定采纳或拒绝这条建议",
        ))

    # ② 本周偏离信号（≤2）：只引事件 reasons 标签（元数据），不搬归因正文
    event_repo = LearningEventRepository(session, user_id=user_id)
    checked = [
        e for e in event_repo.list_recent(user_id, event_type=events.L4_DEVIATION_CHECKED, limit=20)
        if e.occurred_at and e.occurred_at >= week_start
    ][:2]
    for event in checked:
        reasons = (event.payload or {}).get("reasons") or []
        label = "；".join(str(r) for r in reasons[:2]) or "检测到计划偏离"
        materials.blocks.append(CoachBlock(
            label="偏离信号",
            summary=label,
            next_step="打开计划页确认是否需要调整本周任务",
        ))

    # ③ 目标追踪块（进度 + 催办/达成确认 + 干预未决跟进；规则见 goal_tracking）
    materials.blocks.extend(_goal_blocks(session, user_id, now))

    # 附注：弱真值基线（最近一次周体检）
    latest_eval = event_repo.list_recent(user_id, event_type=events.L2_EVAL_WEEKLY, limit=1)
    if latest_eval:
        payload = latest_eval[0].payload or {}
        materials.eval_line = (
            f"判定精度基线：采纳 {payload.get('n_accepted', 0)} / "
            f"忽略 {payload.get('n_ignored', 0)}"
        )

    return materials


def _goal_blocks(session: Session, user_id: str, now: datetime) -> list[CoachBlock]:
    """目标追踪块：进度 + 情景化下一步（催办 > 达成确认 > 常规推进）+ 干预未决跟进。

    全部走 goal_tracking 纯规则，**不写 goal.achieved / plan_task.status**。
    """
    blocks: list[CoachBlock] = []
    snapshot = goal_tracking.goal_snapshot(session, user_id, now)
    if snapshot is not None and snapshot.total:
        if snapshot.days_to_deadline is None:
            timing = ""
        elif snapshot.days_to_deadline >= 0:
            timing = f"（距截止 {snapshot.days_to_deadline} 天）"
        else:
            timing = f"（已过期 {abs(snapshot.days_to_deadline)} 天）"
        nudge = goal_tracking.deadline_nudge(
            snapshot, nudge_days=settings.l4_deadline_nudge_days
        )
        confirm = goal_tracking.completion_confirm(snapshot)
        blocks.append(CoachBlock(
            label="目标进度",
            summary=f"计划任务 {snapshot.done}/{snapshot.total} 已完成{timing}",
            next_step=nudge or confirm or "完成一项本周任务并把它标记为已完成",
        ))
    pending = goal_tracking.pending_adjustment(session, user_id, now)
    if pending:
        blocks.append(CoachBlock(
            label="待决定调整",
            summary=pending,
            next_step="打开计划页决定是否按建议调整",
        ))
    return blocks


def assemble(
    materials: CoachMaterials,
    questions: list | None = None,
    *,
    max_items: int = 3,
) -> CoachDigest | None:
    """纯本地模板拼装。空材料返回 None（无内容不打扰）。questions 元素须有
    question / next_step 属性（L3 DepthQuestion）。"""
    questions = questions or []
    blocks = [b for b in materials.blocks if b.next_step.strip()][:max_items]
    usable_questions = [
        q for q in questions if str(getattr(q, "next_step", "") or "").strip()
    ][:2]
    if not blocks and not usable_questions:
        return None

    now = datetime.now(timezone.utc)
    week_label = f"{now.year}-W{now.isocalendar()[1]:02d}"
    lines: list[str] = []
    for block in blocks:
        lines.append(f"【{block.label}】{block.summary}")
        lines.append(f"  下一步：{block.next_step}")
    if usable_questions:
        lines.append("【本周追问】")
        for q in usable_questions:
            lines.append(f"· {q.question}")
            lines.append(f"  下一步：{q.next_step}")
    if materials.eval_line:
        lines.append(f"（{materials.eval_line}）")

    return CoachDigest(
        week_label=week_label,
        title=f"教练提示 - {week_label}",
        body="\n".join(lines),
    )
