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