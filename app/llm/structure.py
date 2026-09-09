"""结构化输出（ADR-11 落地）：强 Schema 校验 + 失败重试。

reasoning=on 时 DeepSeek 思考模式对 response_format 的兼容存在弱点，
故这里先走自由文本 → 本地 JSON 解析 → Schema 校验；解析/校验失败可选择重试。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.exceptions import LLMError, NonRetryableLLMError

logger = logging.getLogger(__name__)


class JsonParseError(NonRetryableLLMError):
    """模型输出无法解析为合法 JSON，或不符合给定 schema。"""


def parse_json(text: str) -> Any:
    """顽健解析：剥离 markdown 围栏后 json.loads。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return json.loads(t)


def validate(data: Any, validator: callable | None = None) -> Any:
    """按给定 schema（pydantic model 或自定义校验器）校验；通过则返回。"""
    if validator is None:
        return data
    try:
        return validator(data)
    except Exception as exc:
        raise JsonParseError(f"结构校验失败: {exc}") from exc


def parse_structured(text: str, validator: callable | None = None) -> Any:
    try:
        data = parse_json(text)
    except json.JSONDecodeError as exc:
        raise JsonParseError(f"模型输出非 JSON: {exc}") from exc
    return validate(data, validator)