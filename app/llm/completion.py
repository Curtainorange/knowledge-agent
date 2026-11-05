"""模型完成结构：统一承载文本 + token 计数。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    # 命中自动上下文缓存的输入 token 数（DeepSeek 自动缓存）。命中价约为未命中的
    # 1/50，计费必须分开算，否则成本会被严重高估。
    cached_tokens: int = 0
    finish_reason: str = "stop"
    model: str = ""
    reasoning: bool = False
    # 模型请求调用的工具（function calling）。空表示这一次只出了文本。
    # 每项形如 {"id": "...", "name": "web_search", "arguments": "{...}"}——
    # 参数保持**原始 JSON 字符串**不在这里解析：解析失败要由调用方决定怎么兜，
    # 而不是让模型层替业务层吞掉一个坏参数。
    tool_calls: list[dict] = field(default_factory=list)