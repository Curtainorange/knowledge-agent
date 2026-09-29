"""任务处理器：把异步任务名映射到具体能力调用。

只做「取参数 → 调编排层 → 记日志」，不含业务逻辑本身；失败直接抛异常，
交给框架记 last_error 并重试（重试耗尽进死信）。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.workers.tasks import register

logger = logging.getLogger(__name__)


@register("l2_scan")
def l2_scan(payload: dict, session: Session) -> None:
    """对单个用户执行一次 L2 增量扫描（主张提取 → 候选对 → LLM 判定 → 冲突入库）。"""
    from app.agent.l2_orchestrator import L2Orchestrator
    from app.llm.gateway import ModelGateway

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise ValueError("l2_scan 缺少 user_id")

    result = L2Orchestrator(ModelGateway(), session).scan(user_id=user_id)
    logger.info(
        "l2 scan done user=%s reason=%s items=%d claims=%d judged=%d conflicts=%d suppressed=%d",
        user_id, payload.get("reason", "-"), result.scanned_items, result.claims_extracted,
        result.pairs_judged, result.conflicts_found, result.conflicts_suppressed,
    )


@register("agent_turn")
def agent_turn(payload: dict, session: Session) -> None:
    """对话里发起的后台回合：能力执行（L2 扫描 / L3 简报 / L4 计划与偏离 / L5 诊断）
    或慢的卡片操作（按建议重排计划）。

    与其它处理器只记日志不同，它必须**把结果写回那条 pending 消息**——
    否则用户只会看到一条永远在转圈的卡片。执行逻辑与同步路径共用
    `turns.execute_capability` / `turns.execute_action`，异步只改「在哪儿跑」。
    """
    from app.agent import turns
    from app.llm.gateway import ModelGateway

    kind = str(payload.get("kind") or "capability")
    turn_id = str(payload.get("turn_id") or "")
    user_id = str(payload.get("user_id") or "")
    conversation_id = str(payload.get("conversation_id") or "")
    if not (turn_id and user_id and conversation_id):
        raise ValueError("agent_turn 缺少 turn_id / user_id / conversation_id")

    if kind == "action":
        reply, card = turns.execute_action(
            str(payload.get("action") or ""),
            user_id=user_id,
            session=session,
            card_key=str(payload.get("card_key") or ""),
            target_id=str(payload.get("target_id") or ""),
            value=str(payload.get("value") or ""),
        )
    else:
        capability = str(payload.get("capability") or "")
        if not capability:
            raise ValueError("agent_turn 缺少 capability")
        reply, card = turns.execute_capability(
            capability, user_id=user_id, session=session, gateway=ModelGateway(),
            key=turn_id, args=payload.get("args") or {},
        )

    written = turns.finish_turn(
        session, user_id=user_id, conversation_id=conversation_id,
        turn_id=turn_id, reply=reply, card=card,
    )
    logger.info(
        "agent turn done user=%s kind=%s turn=%s written=%s",
        user_id, kind, turn_id, written,
    )


@register("push_weekly_digest")
def push_weekly_digest(payload: dict, session: Session) -> None:
    """周简报推送：组装本周冲突/新增/活跃度，有内容才推（无内容不打扰）。"""
    from app.agent.push_service import PushService

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise ValueError("push_weekly_digest 缺少 user_id")

    svc = PushService(session)
    digest = svc.build_weekly_digest(user_id=user_id)
    if digest.conflict_total == 0 and digest.new_items == 0:
        logger.info("push weekly skipped user=%s (本周无冲突且无新知识)", user_id)
        return
    outcome = svc.enqueue(
        user_id=user_id, push_type="brief",
        title=digest.title, body=digest.body, subject=digest.week_label,
    )
    logger.info("push weekly user=%s status=%s", user_id, outcome.status)


@register("push_monthly_health")
def push_monthly_health(payload: dict, session: Session) -> None:
    """月学习健康报告：L5 归因诊断，有诊断结论才推。"""
    from app.agent.l5_orchestrator import L5Orchestrator
    from app.agent.push_service import PushService

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise ValueError("push_monthly_health 缺少 user_id")

    result = L5Orchestrator(session=session).diagnose(user_id=user_id)
    if result.state != "ok":
        logger.info("push monthly skipped user=%s state=%s", user_id, result.state)
        return
    svc = PushService(session)
    outcome = svc.enqueue(
        user_id=user_id, push_type="diagnosis",
        title=f"学习健康报告 · {result.pattern or '诊断'}",
        body=f"{result.root_cause}\n建议：{result.suggested_action}",
        subject=result.diagnosis_id,
    )
    logger.info("push monthly user=%s status=%s", user_id, outcome.status)


@register("l4_deviation_check")
def l4_deviation_check(payload: dict, session: Session) -> None:
    """L4 偏离日巡检：本地信号零 LLM 排查，确认偏离才归因，归因即推干预。

    成本/打扰双护栏：近 `l4_intervention_repeat_days` 天已归因过（有
    L4_DEVIATION_CHECKED 事件）就跳过——持续偏离时 LLM ≤1 次/护栏期，
    同时防干预刷屏。no_plan / no_deviation 是零 LLM 快路径。
    """
    from datetime import datetime, timedelta, timezone

    from app.agent.l4_orchestrator import L4Orchestrator
    from app.agent.push_service import PushService
    from app.core.config import settings
    from app.domain.repositories.learning_event_repository import LearningEventRepository
    from app.feedback import events
    from app.llm.gateway import ModelGateway

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise ValueError("l4_deviation_check 缺少 user_id")

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=max(1, int(settings.l4_intervention_repeat_days))
    )
    recent = LearningEventRepository(session, user_id=user_id).list_since(
        user_id, since=since, event_type=events.L4_DEVIATION_CHECKED, limit=1
    )
    if recent:
        logger.info("l4 patrol skipped user=%s (近 %d 天已归因)", user_id, settings.l4_intervention_repeat_days)
        return

    result = L4Orchestrator(ModelGateway(), session).check_deviation(user_id=user_id)
    if result.state != "ok":
        logger.info("l4 patrol user=%s state=%s", user_id, result.state)
        return

    analysis = result.analysis
    body = (
        f"{analysis.root_cause}\n"
        f"建议：{analysis.adjustment}\n"
        f"预期收益：{analysis.expected_gain}"
    )
    outcome = PushService(session).enqueue(
        user_id=user_id, push_type="coach",
        title="计划偏离提醒", body=body,
        subject=f"deviation:{result.plan_id}:{datetime.now(timezone.utc):%Y%m%d}",
    )
    logger.info("l4 patrol user=%s push=%s", user_id, outcome.status)


@register("l2_eval_weekly")
def l2_eval_weekly(payload: dict, session: Session) -> None:
    """L2 弱真值周体检：零 LLM，把 accepted/ignored 精度与校准分桶写成事件供教练引用。"""
    from app.agent.l2_eval import weak_label_metrics
    from app.domain.repositories.conflict_repository import ConflictRepository
    from app.feedback import events

    user_id = str(payload.get("user_id") or "")
    if not user_id:
        raise ValueError("l2_eval_weekly 缺少 user_id")

    conflicts = ConflictRepository(session, user_id=user_id).list_by_user(user_id, limit=1000)
    metrics = weak_label_metrics([(c.user_state, c.confidence) for c in conflicts])
    events.record(
        session, user_id=user_id, event_type=events.L2_EVAL_WEEKLY,
        payload={
            "precision": metrics["precision"],
            "n_accepted": metrics["n_accepted"],
            "n_ignored": metrics["n_ignored"],
            "calibration_by_bucket": metrics["calibration_by_bucket"],
        },
    )
    logger.info(
        "l2 eval weekly user=%s precision=%.3f accepted=%d ignored=%d",
        user_id, metrics["precision"], metrics["n_accepted"], metrics["n_ignored"],
    )