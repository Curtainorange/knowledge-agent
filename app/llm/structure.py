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


# 修复回合的追加指令。措辞针对实测的两种破损：把 JSON 包在解释性文字里、
# 以及字符串值里的引号没转义——都要求「完整重出」而不是「打补丁」。
_REPAIR_INSTRUCTION = (
    "你上一条回复没有通过 JSON 结构校验。失败原因：{err}。\n"
    "请重新输出**完整**的修正结果：只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码围栏，"
    "字段必须齐全、类型正确，字符串里的引号按 JSON 规则转义。"
)


def build_repair_messages(messages: list[dict], broken_text: str, error: str) -> list[dict]:
    """构造「修复回合」的消息：把破损输出当作上一轮回复，再要求模型只输出修正后的结果。

    为什么值得多花一次调用：结构化输出失败的下游代价是**整条能力降级**——L3 简报退化成
    空简报、L2 判定直接丢弃该候选对、分流回落通用对话——而修复回合只是一次短调用。
    实测 MiMo 在自由文本模式下约 1/6 概率吐出破损 JSON（详见 dev_logs/复盘与教训.md #16）。
    """
    instruction = _REPAIR_INSTRUCTION.replace("{err}", (error or "未知")[:300])
    return [
        *messages,
        {"role": "assistant", "content": broken_text},
        {"role": "user", "content": instruction},
    ]