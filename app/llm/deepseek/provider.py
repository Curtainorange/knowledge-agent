"""DeepSeekProvider：真实 LLM 调用（OpenAI 兼容协议）。

- 使用 openai SDK 指向 DEEPSEEK_BASE_URL，不引入额外 SDK。
- 单一模型 deepseek-flash，reasoning=on/off 在请求内切换思考模式。
- 连接参数、错误译码、缓存命中解析统一复用 OpenAICompatProvider。

注意：deepseek-flash reasoning 的精确线上字段以接入实测校准。
DEEPSEEK_API_KEY 仅按需求安全-2 使用，绝不写入日志。
"""
from __future__ import annotations

from app.core.config import settings
from app.llm.openai_compat import OpenAICompatProvider


class DeepSeekProvider(OpenAICompatProvider):
    def __init__(self):
        super().__init__(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=settings.deepseek_timeout_seconds,
        )

    @property
    def default_model(self) -> str:
        """本供应商默认模型名（MiMo 失败回退到 DeepSeek 时，网关改用它）。"""
        return settings.deepseek_model

    def _thinking_extra_body(self, reasoning: bool) -> dict | None:
        # DeepSeek 思考默认关闭，仅 reasoning=on 时开启；off 时不注入字段，保持默认。
        if reasoning:
            return {"reasoning": True, "reasoning_effort": "medium"}
        return None
