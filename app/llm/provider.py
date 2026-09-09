"""LLM Provider 抽象接口（§6.7 模型抽象接口落地）。

业务层只面向此抽象，使 DeepSeek 可无感替换/新增服务商（可替换性铁律）。
实现类：DeepSeekProvider（真实）、MockProvider（测试/本地确定性）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.llm.completion import Completion


class LLMProvider(ABC):
    """统一模型调用契约。"""

    @abstractmethod
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
        """对话/补全；reasoning 控制思考模式开关；tools = function calling 白名单。"""