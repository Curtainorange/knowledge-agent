"""OpenAI 兼容协议供应商基类。

DeepSeek 与小米 MiMo 都是 OpenAI Chat Completions 兼容接口，业务差异只在三处：
1. 连接参数（base_url / api_key / timeout 来源不同）；
2. 思考模式开关的请求体字段（DeepSeek: `reasoning`；MiMo: `thinking.type`，且默认开启）；
3. 缓存命中 token 的 usage 字段名（可能不同）。

子类只需提供连接参数并覆写 `_thinking_extra_body()`，其余请求/错误译码/成本字段解析全复用。
"""
from __future__ import annotations

import logging

import httpx
import openai
from openai import OpenAI

from app.llm.completion import Completion
from app.llm.exceptions import LLMError, NonRetryableLLMError, RetryableLLMError
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# OpenAI API 的 HTTP 状态 → 是否可重试
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class OpenAICompatProvider(LLMProvider):
    """基于 openai SDK 的 OpenAI 兼容供应商基类。"""

    def __init__(self, *, api_key: str, base_url: str, timeout: float):
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def _thinking_extra_body(self, reasoning: bool) -> dict | None:
        """返回思考模式开关的 extra_body；None 表示不注入该字段（用供应商默认）。

        子类按各自字段语义覆写：
        - DeepSeek：思考默认关，reasoning=on 时塞 {"reasoning": True, ...}，off 时返回 None。
        - MiMo：思考默认开，必须显式 enabled/disabled 才能控成本。
        """
        return None

    def chat(
        self,
        *,
        model: str,
        reasoning: bool,
        messages: list[dict],
        tools: list | None = None,
        response_format: dict | None = None,
        task_type: str = "default",
    ) -> Completion:
        kwargs: dict = {"model": model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if response_format:
            kwargs["response_format"] = response_format
        # 思考模式开关：openai SDK 不认识 reasoning/thinking 顶层参数，
        # 通过 extra_body 塞进 JSON 请求体（服务器接受未知字段时静默忽略/启用）。
        extra = self._thinking_extra_body(reasoning)
        if extra is not None:
            kwargs["extra_body"] = extra

        try:
            resp = self._client.chat.completions.create(**kwargs)
        except openai.APIStatusError as exc:
            raise self._decode_status(exc) from exc
        except (openai.APIConnectionError, httpx.TimeoutException, openai.APITimeoutError) as exc:
            raise RetryableLLMError("连接/超时失败，可重试") from exc
        except openai.OpenAIError as exc:
            raise NonRetryableLLMError(f"非可重试模型错误: {exc}") from exc

        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content if choice and choice.message else "") or ""
        if not text and choice and getattr(choice.message, "tool_calls", None):
            # 函数调用场景：把 tool_call 转成可读摘要，P0 阶段不执行工具
            calls = ", ".join(
                f"{tc.function.name}({tc.function.arguments})" for tc in choice.message.tool_calls
            )
            text = f"[tool_request] {calls}"

        usage = resp.usage
        return Completion(
            text=text,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cached_tokens=self._cached_tokens(usage),
            finish_reason=choice.finish_reason if choice else "stop",
            model=model,
            reasoning=reasoning,
        )

    @staticmethod
    def _cached_tokens(usage) -> int:
        """取「命中自动上下文缓存」的输入 token 数。

        OpenAI 兼容字段是 `prompt_tokens_details.cached_tokens`，DeepSeek 另在
        `usage.prompt_cache_hit_tokens` 上暴露同一信息；两个都试，取到即止
        （取不到按 0 计，宁可少算优惠也不虚报）。
        """
        if usage is None:
            return 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        if cached:
            return cached
        return int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)

    @staticmethod
    def _decode_status(exc: openai.APIStatusError) -> LLMError:
        if exc.status_code in _RETRYABLE_STATUS:
            return RetryableLLMError(f"可重试状态 {exc.status_code}: {exc}")
        return NonRetryableLLMError(f"非可重试状态 {exc.status_code}: {exc}")
