"""MockProvider：测试/本地确定性供应商。

根据架构文档测试铁律——禁止调用真实 API，用固定响应保证确定性与零成本。
（需求 R 相关：MockProvider 在 CI 与本地演示中取代真实 DeepSeek）
"""
from __future__ import annotations

from app.llm.completion import Completion
from app.llm.provider import LLMProvider

_PREFIX = {
    "multi_turn_dialogue": "[mock · 多轮对话]",
    "default": "[mock]",
}


class MockProvider(LLMProvider):
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
        prefix = _PREFIX.get(task_type, _PREFIX["default"])
        echo = (messages[-1]["content"] if messages else "").strip()
        mode = "reasoner=on" if reasoning else "reasoner=off"
        text = f"{prefix} [{mode} @{model}] 收到：{echo}"
        # 确定性 token 计数，便于断言成本埋点
        prompt_tokens = sum(len(m.get("content", "")) for m in messages) // 2 + 1
        completion_tokens = len(text) // 2 + 1
        return Completion(
            text=text,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason="stop",
            model=model,
            reasoning=reasoning,
        )