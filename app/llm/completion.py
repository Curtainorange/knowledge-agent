"""模型完成结构：统一承载文本 + token 计数。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str = "stop"
    model: str = ""
    reasoning: bool = False