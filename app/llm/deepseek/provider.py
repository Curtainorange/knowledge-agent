"""DeepSeekProvider：真实 LLM 调用（OpenAI 兼容协议）。

- 使用 openai SDK 指向 DEEPSEEK_BASE_URL，不引入额外 SDK。
- 单一模型 deepseek-v4-flash，reasoning=on/off 在请求内切换思考模式。
- 错误译码：可重试状态（429/5xx/超时）→ RetryableLLMError，其余 → NonRetryableLLMError。

注意：deepseek-v4-flash reasoning 的精确线上字段以接入实测校准。
DEEPSEEK_API_KEY 仅按需求安全-2 使用，绝不写入日志。
"""
from __future__ import annotations

import logging

import httpx
import openai
from openai import OpenAI

from app.core.config import settings
from app.llm.completion import Completion
from app.llm.exceptions import LLMError, NonRetryableLLMError, RetryableLLMError
from app.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

# OpenAI API 的 HTTP 状态 → 是否可重试
_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


class DeepSeekProvider(LLMProvider):
    def __init__(self):
        self._client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
        )

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
        # 思考模式切换。单模型 + 每请求开关：openai SDK 不认识 reasoning 顶层参数，
        # 通过 extra_body 塞进 JSON 请求体（服务器接受未知字段时静默忽略/启用）。
        # 字段名以接入实测为准，必要时收敛到配置项。
        if reasoning:
            kwargs["extra_body"] = {"reasoning": True, "reasoning_effort": "medium"}

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

        return Completion(
            text=text,
            prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
            completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
            finish_reason=choice.finish_reason if choice else "stop",
            model=model,
            reasoning=reasoning,
        )

    @staticmethod
    def _decode_status(exc: openai.APIStatusError) -> LLMError:
        if exc.status_code in _RETRYABLE_STATUS:
            return RetryableLLMError(f"可重试状态 {exc.status_code}: {exc}")
        return NonRetryableLLMError(f"非可重试状态 {exc.status_code}: {exc}")