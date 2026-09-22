"""LLM Provider 抽象接口（§6.7 模型抽象接口落地）。

业务层只面向此抽象，使 DeepSeek 可无感替换/新增服务商（可替换性铁律）。
实现类：DeepSeekProvider（真实）、MockProvider（测试/本地确定性）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.llm.completion import Completion


class LLMProvider(ABC):
    """统一模型调用契约。"""

    # 是否支持原生 JSON 约束（OpenAI 兼容的 response_format）。
    # JSON 任务的提示词虽写了「输出严格 JSON」，但自由文本仍会偶发破损：
    # 实测 MiMo 会间歇性吐出未转义引号（JSON 解析失败），或把嵌套对象压成字符串数组
    # （JSON 能解析但结构校验失败）。交给服务端约束比事后修补可靠。
    #
    #   supports_json_object → response_format={"type":"json_object"}，保证「合法 JSON」
    #   supports_json_schema → response_format={"type":"json_schema",...}，连结构一起约束
    #
    # 两个都默认 False：开启前需确认该供应商在思考模式下也兼容 response_format
    # （DeepSeek 在 reasoning=on 时对 response_format 有已知兼容弱点，故保持关闭）。
    supports_json_object: bool = False
    supports_json_schema: bool = False

    # 可选属性 `default_model`（字符串）：本供应商自己的默认模型名。
    # 网关执行供应商回退时改用它——把主供应商的模型名（如 mimo-v2.6-flash）发给
    # 备用供应商会被判 Unsupported model，等于回退必然失败。未提供时回落到策略表模型名。

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