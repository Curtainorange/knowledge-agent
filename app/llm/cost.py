"""计费：token → 估算费用，统一写 CostLog。

三件事必须一起做对，否则成本账目会失真一个量级：
1. **单价随时段变**：DeepSeek 高峰（北京时间工作日 9-12、14-18）价是空闲的两倍，
   配置里存空闲价，高峰按倍率放大。
2. **缓存命中与未命中分开计价**：命中价约为未命中的 1/50，而 L1/L2 的 system
   提示词前缀稳定、命中率不低；只按未命中价算会严重高估。
3. **推理 token 计入输出**：思考模式（reasoning=on）的输出远高于非思考模式，
   这部分天然落在 completion_tokens 里，不做额外处理但要心里有数。

单价全部来自配置（.env 可覆盖），接入实测校准后无需改代码。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core import trace
from app.core.config import settings
from app.domain.repositories.cost_log_repository import CostLogRepository
from app.llm.completion import Completion

logger = logging.getLogger(__name__)

# 北京时间固定偏移：中国无夏令时，固定 UTC+8 即精确；
# 不用 zoneinfo 是因为 Windows 上需额外 tzdata 包，徒增依赖
_CST = timezone(timedelta(hours=8))


def _peak_windows() -> list[tuple[int, int]]:
    """解析 deepseek_peak_hours（形如 "9-12,14-18"）；非法片段直接忽略。"""
    windows: list[tuple[int, int]] = []
    for chunk in (settings.deepseek_peak_hours or "").split(","):
        start_text, _, end_text = chunk.strip().partition("-")
        try:
            start, end = int(start_text), int(end_text)
        except ValueError:
            continue
        if 0 <= start < end <= 24:
            windows.append((start, end))
    return windows


def is_peak_time(now: datetime | None = None) -> bool:
    """当前是否处于高峰计费时段（北京时间，仅工作日）。"""
    local = (now or datetime.now(timezone.utc)).astimezone(_CST)
    if local.weekday() >= 5:  # 周六周日全天空闲
        return False
    return any(start <= local.hour < end for start, end in _peak_windows())


def _estimate_cost(prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    """按每百万 token 单价估算人民币费用（输入区分缓存命中 / 未命中）。"""
    multiplier = settings.deepseek_peak_multiplier if is_peak_time() else 1.0
    cached = max(0, min(int(cached_tokens), int(prompt_tokens)))
    uncached = max(0, int(prompt_tokens) - cached)

    cost = (
        uncached * settings.deepseek_price_input_per_1m
        + cached * settings.deepseek_price_cache_hit_per_1m
        + int(completion_tokens) * settings.deepseek_price_output_per_1m
    )
    return cost * multiplier / 1_000_000


def record_cost(
    session: Session | None,
    *,
    user_id: str,
    task_type: str,
    model: str,
    reasoning: bool,
    completion: Completion,
    prompt_version: str = "",
) -> float:
    """记录一次模型调用的 token 与估算费用。无 session 时仅记日志（回退）。"""
    cost = _estimate_cost(
        completion.prompt_tokens, completion.completion_tokens, completion.cached_tokens
    )
    rid = trace.get_request_id() or "-"
    if session is not None:
        CostLogRepository(session).create(
            user_id=user_id,
            task_type=task_type,
            model=model,
            reasoning=reasoning,
            prompt_version=prompt_version,
            request_id=rid,
            prompt_tokens=completion.prompt_tokens,
            completion_tokens=completion.completion_tokens,
            cached_tokens=completion.cached_tokens,
            estimated_cost=cost,
        )
    else:
        logger.info(
            "model cost (no db) task=%s model=%s reasoning=%s prompt_ver=%s in=%d cached=%d out=%d cost=%.6f req=%s",
            task_type, model, reasoning, prompt_version, completion.prompt_tokens,
            completion.cached_tokens, completion.completion_tokens, cost, rid,
        )
    return cost