"""计费：token → 估算费用，统一写 CostLog。

单价以 .env 配置注入（DEEPSEEK_PRICE_*_PER_1M），接入实测后回填，避免代码固锁失真。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.core import trace
from app.core.config import settings
from app.domain.repositories.cost_log_repository import CostLogRepository
from app.llm.completion import Completion

logger = logging.getLogger(__name__)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按每百万 token 单价估算人民币费用。"""
    input_cost = prompt_tokens * settings.deepseek_price_input_per_1m / 1_000_000
    output_cost = completion_tokens * settings.deepseek_price_output_per_1m / 1_000_000
    return input_cost + output_cost


def record_cost(
    session: Session | None,
    *,
    user_id: str,
    task_type: str,
    model: str,
    reasoning: bool,
    completion: Completion,
) -> float:
    """记录一次模型调用的 token 与估算费用。无 session 时仅记日志（回退）。"""
    cost = _estimate_cost(completion.prompt_tokens, completion.completion_tokens)
    rid = trace.get_request_id() or "-"
    if session is not None:
        CostLogRepository(session).create(
            user_id=user_id,
            task_type=task_type,
            model=model,
            reasoning=reasoning,
            request_id=rid,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            estimated_cost=cost,
        )
    else:
        logger.info(
            "model cost (no db) task=%s model=%s reasoning=%s in=%d out=%d cost=%.6f req=%s",
            task_type, model, reasoning, completion.prompt_tokens,
            completion.completion_tokens, cost, rid,
        )
    return cost