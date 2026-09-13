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


def _strip_fence(text: str) -> str:
    """剥离 markdown 代码围栏（模型常把 JSON 包在 ```json ... ``` 里）。"""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_json(text: str) -> Any:
    """顽健解析：剥围栏 → 取最外层对象 → 宽容解析。

    真实模型（尤其长中文句子）经常把换行直接写进字符串值里，标准 JSON 解析会报
    `Expecting ',' delimiter`；`strict=False` 正好允许字符串内出现裸控制字符。
    另外模型偶发在 JSON 前后夹带解释性文字，因此再退一步截取最外层花括号试一次。

    两层退让都失败才抛错——解析成功率直接决定 L1/L2/L3 的产出率，
    每次“非 JSON”都是一次白花的模型调用。
    """
    cleaned = _strip_fence(text)
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        inner = cleaned[start : end + 1]
        if inner != cleaned:
            candidates.append(inner)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


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