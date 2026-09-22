"""MiMoProvider：小米 MiMo 真实 LLM 调用（OpenAI 兼容协议）。

- 使用 openai SDK 指向 MIMO_BASE_URL（默认 https://api.xiaomimimo.com/v1）。
- 思考开关字段与 DeepSeek 不同：`thinking.type` = enabled | disabled，且**默认开启**，
  故 reasoning=off 时也必须显式 `disabled` 才能控成本（否则每次调用都走思考模式）。
- 连接参数、错误译码、缓存命中解析统一复用 OpenAICompatProvider。

MIMO_API_KEY 仅按需求安全-2 使用，绝不写入日志。
按量付费 key 为 sk- 前缀；订阅版 Token Plan 是 tp- 前缀且 base_url 不同，两者不可混用。
"""
from __future__ import annotations

from app.core.config import settings
from app.llm.openai_compat import OpenAICompatProvider


class MiMoProvider(OpenAICompatProvider):
    # MiMo 支持原生 JSON 约束（实测 response_format 的 json_object 与 json_schema 均可用）。
    # 开启后由网关给「期望严格 JSON」的任务自动带上：优先 json_schema（连结构一起约束，
    # 实测 L3 简报 6/6 通过），无 schema 时退到 json_object（仅保证合法 JSON，实测 5/6）。
    # 纯对话任务（multi_turn_dialogue）不受影响。
    supports_json_object = True
    supports_json_schema = True

    def __init__(self):
        super().__init__(
            api_key=settings.mimo_api_key,
            base_url=settings.mimo_base_url,
            timeout=settings.mimo_timeout_seconds,
        )

    def _thinking_extra_body(self, reasoning: bool) -> dict | None:
        # MiMo 思考默认开启：两个方向都显式指定，off 时也塞 disabled 以省成本。
        return {"thinking": {"type": "enabled" if reasoning else "disabled"}}

    @property
    def default_model(self) -> str:
        """本供应商默认模型名（网关路由与成本记录用）。"""
        return settings.mimo_model
