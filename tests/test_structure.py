"""结构化输出解析测试（ADR-11）。

解析成功率直接决定 L1/L2/L3 的产出率：每一次「模型输出非 JSON」都是一次白花的
调用，所以这里把真实踩过的几种脏输出形态全部钉住。
"""
from __future__ import annotations

import json

import pytest

from app.llm.structure import JsonParseError, parse_json, parse_structured


def test_plain_json():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_markdown_fenced_json():
    text = '```json\n{"a": 1, "b": "值"}\n```'
    assert parse_json(text) == {"a": 1, "b": "值"}


def test_fenced_without_language_tag():
    text = '```\n{"ok": true}\n```'
    assert parse_json(text) == {"ok": True}


def test_raw_newline_inside_string_value():
    """真实故障形态：模型在字符串值里直接换行，标准解析报 Expecting ',' delimiter。

    DeepSeek 长中文回答很常见，strict=False 允许字符串内出现裸控制字符。
    """
    text = '{"question": "第一行\n第二行", "evidence": "6 篇"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(text)  # 标准解析确实会失败——证明这个用例有意义
    assert parse_json(text) == {"question": "第一行\n第二行", "evidence": "6 篇"}


def test_json_wrapped_in_prose():
    """模型话多时会在 JSON 前后夹带说明文字，截取最外层花括号仍可解析。"""
    text = '好的，以下是结果：\n{"a": [1, 2], "b": {"c": 3}}\n希望有帮助。'
    assert parse_json(text) == {"a": [1, 2], "b": {"c": 3}}


def test_nested_object_with_prose_and_newlines():
    text = '说明：\n{"topics": [{"topic": "健身", "count": 2}],\n "overview": "两条线\n都浅"}'
    parsed = parse_json(text)
    assert parsed["topics"][0]["topic"] == "健身"
    assert parsed["overview"] == "两条线\n都浅"


def test_invalid_json_raises_parse_error():
    with pytest.raises(JsonParseError):
        parse_structured("完全不是 JSON", validator=lambda d: d)


def test_validator_failure_becomes_parse_error():
    with pytest.raises(JsonParseError):
        parse_structured('{"a": 1}', validator=lambda d: d["missing_key"])


def test_structured_returns_validated_value():
    assert parse_structured('{"a": 2}', validator=lambda d: d["a"] * 10) == 20