"""模型完成结构：统一承载文本 + token 计数。"""
from __future__ import annotations

from dataclasses import dataclass


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