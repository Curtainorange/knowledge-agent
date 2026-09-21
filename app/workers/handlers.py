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


@register("agent_capability")
def agent_capability(payload: dict, session: Session) -> None:
    """对话里发起的重能力（L2 扫描 / L3 简报 / L5 诊断）。

    与其它处理器只记日志不同，它必须**把结果写回那条 pending 消息**——
    否则用户只会看到一条永远在转圈的卡片。执行逻辑与同步路径共用
    `turns.execute_capability`，异步只改「在哪儿跑」，不改「跑什么」。
    """
    from app.agent import turns
    from app.llm.gateway import ModelGateway

    turn_id = str(payload.get("turn_id") or "")
    user_id = str(payload.get("user_id") or "")
    conversation_id = str(payload.get("conversation_id") or "")
    capability = str(payload.get("capability") or "")
    if not (turn_id and user_id and conversation_id and capability):
        raise ValueError("agent_capability 缺少 turn_id / user_id / conversation_id / capability")

    reply, card = turns.execute_capability(
        capability, user_id=user_id, session=session, gateway=ModelGateway(), key=turn_id
    )
    written = turns.finish_turn(
        session, user_id=user_id, conversation_id=conversation_id,
        turn_id=turn_id, reply=reply, card=card,
    )
    logger.info(
        "agent turn done user=%s capability=%s turn=%s written=%s",
        user_id, capability, turn_id, written,
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