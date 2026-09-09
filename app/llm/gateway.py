"""统一模型网关（§3.1 唯一收口点）。

职责：策略路由（task_type → reasoning 开关）→ Provider 调用 → 重试 → 成本落库。
业务层不感知供应商细节；全链路 request_id 在这里贯穿。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core import trace
from app.core.config import settings
from app.llm import cost as cost_service
from app.llm.completion import Completion
from app.llm.exceptions import RetryableLLMError
from app.llm.provider import LLMProvider
from app.llm.retry import with_retry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Strategy:
    task_type: str
    reasoning: bool
    model: str  # 空 → 运行时回落 settings.deepseek_model


# 静态映射（配表驱动，可热更新；缺省为"默认对话 reasoning=off"）
STRATEGY_TABLE: dict[str, Strategy] = {
    "default": Strategy("default", False, ""),
    "multi_turn_dialogue": Strategy("multi_turn_dialogue", True, ""),
    "deep_reasoning": Strategy("deep_reasoning", True, ""),
    "conflict_detection": Strategy("conflict_detection", True, ""),
    "plan_generation": Strategy("plan_generation", True, ""),
    "causal_reasoning": Strategy("causal_reasoning", True, ""),
    "cognitive_brief": Strategy("cognitive_brief", True, ""),
    "batch_extraction": Strategy("batch_extraction", False, ""),
    "topic_analysis": Strategy("topic_analysis", False, ""),
}


def strategy_for(task_type: str = "default") -> Strategy:
    return STRATEGY_TABLE.get(task_type, STRATEGY_TABLE["default"])


def _build_provider() -> LLMProvider:
    """按配置选择供应商：无 KEY → MockProvider（确定性、零成本）。"""
    if settings.model_provider == "deepseek":
        from app.llm.deepseek.provider import DeepSeekProvider
        return DeepSeekProvider()
    from app.llm.deepseek.mock import MockProvider
    logger.info("未配置 DEEPSEEK_API_KEY，网关路由到 MockProvider")
    return MockProvider()


class ModelGateway:
    """所有模型调用的唯一入口（可替换性抽象落地的门面）。"""

    def __init__(self, provider: LLMProvider | None = None):
        self._provider = provider or _build_provider()

    def route(self, task_type: str = "default") -> Strategy:
        s = strategy_for(task_type)
        return Strategy(s.task_type, s.reasoning, s.model or settings.deepseek_model)

    def chat(
        self,
        *,
        task_type: str = "default",
        messages: list[dict],
        user_id: str = "anonymous",
        session: Session | None = None,
        tools: list | None = None,
        response_format: dict | None = None,
    ) -> Completion:
        """发起一次模型调用：路由 → 重试 → 成本埋点。"""
        strat = self.route(task_type)
        completion = self._call_with_retry(strat, messages, tools, response_format)
        cost_service.record_cost(
            session,
            user_id=user_id,
            task_type=strat.task_type,
            model=strat.model,
            reasoning=strat.reasoning,
            completion=completion,
        )
        return completion

    def _call_with_retry(self, strat: Strategy, messages, tools, response_format) -> Completion:
        def invoke() -> Completion:
            return self._provider.chat(
                model=strat.model,
                reasoning=strat.reasoning,
                messages=messages,
                tools=tools,
                response_format=response_format,
                task_type=strat.task_type,
            )

        def on_retry(attempt: int, exc: BaseException) -> None:
            logger.warning(
                "llm retry attempt=%d task=%s err=%s req=%s", attempt, strat.task_type, exc,
                trace.get_request_id(),
            )

        start = time.monotonic()
        result = with_retry(invoke, attempts=3, retry_exceptions=(RetryableLLMError,), on_retry=on_retry)
        logger.info(
            "llm ok task=%s model=%s reasoning=%s in=%d out=%d dur=%.0fms req=%s",
            strat.task_type, strat.model, strat.reasoning,
            result.prompt_tokens, result.completion_tokens,
            (time.monotonic() - start) * 1000, trace.get_request_id(),
        )
        return result