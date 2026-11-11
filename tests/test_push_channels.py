"""推送通道发送器测试（ADR-14 的微信通道）。

只测 send_wechat（httpx.MockTransport 注入）：缺 key / 非法前缀 / 异常响应 / 网络错误
都降级为 False，不抛异常。enqueue 的旁路分发行为见 tests/test_push.py。
"""
from __future__ import annotations

import urllib.parse

import httpx
import pytest

from app.core.config import settings
from app.push import channels


# ---------- 测试环境自保：绝不能把测试消息推到真实微信 ----------


def test_wechat_channel_is_off_in_tests():
    """回归：本机 .env 里通道是开的、Key 是真的，测试必须自己关掉。

    2026-11-11 现场：每跑一次 pytest，用户微信就收到几条标题为 t / 周简报 /
    观点冲突：A ⇄ B 的空推送，正是这些用例漏关通道导致的。见 tests/conftest.py。
    """
    assert settings.push_wechat_enabled is False
    assert settings.serverchan_api_key == ""


# ---------- 标题合成（免费版只显示标题）----------


@pytest.mark.parametrize(
    "title, body, expect",
    [
        ("周简报", "本周 3 条知识", "周简报｜本周 3 条知识"),
        # 正文已包含标题 → 不重复拼
        ("本周认知简报", "本周认知简报：新增 3 条", "本周认知简报：新增 3 条"),
        # 只有标题
        ("只有标题", "", "只有标题"),
        # 全空也要有兜底标题（title 必填）
        ("", "", "认知副驾"),
    ],
)
def test_compose_title(title, body, expect):
    assert channels.compose_title(title, body) == expect


def test_compose_title_never_exceeds_limit():
    """Server酱 title 上限 32 字；正文摘要必须挤进去而不是被服务端硬截。"""
    merged = channels.compose_title("很长的标题" * 5, "很长的正文内容" * 5)
    assert len(merged) <= channels.TITLE_MAX
    assert merged.endswith("…")


def test_compose_title_strips_newlines():
    assert "\n" not in channels.compose_title("标题\n第二行", "正文\r\n第二行")


# ---------- 发送 ----------


def test_send_wechat_no_key_returns_false():
    assert channels.send_wechat("t", "b", sendkey="") is False


def test_send_wechat_rejects_unknown_key_prefix():
    assert channels.send_wechat("t", "b", sendkey="not-a-key") is False


def test_send_wechat_posts_and_detects_success():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(urllib.parse.parse_qsl(request.content.decode("utf-8")))
        return httpx.Response(200, json={"code": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert channels.send_wechat("学习健康报告", "正文内容", sendkey="SCT123", http=client) is True
    assert "sctapi.ftqq.com" in seen["url"]
    assert "/SCT123.send" in seen["url"]
    # desp 必须照发：免费版卡片不显示，但付费账号/详情页要用
    assert seen["form"]["desp"] == "正文内容"
    assert seen["form"]["title"] == "学习健康报告｜正文内容"


def test_send_wechat_truncates_title_and_strips_newlines():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    long_title = "很长的标题" * 20
    assert channels.send_wechat(long_title + "\n第二行", "body", sendkey="SCT1", http=client) is True
    assert "\n" not in seen["body"]


def test_send_wechat_bad_code_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 1024})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert channels.send_wechat("t", "b", sendkey="SCT1", http=client) is False


def test_send_wechat_network_error_returns_false():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert channels.send_wechat("t", "b", sendkey="SCT1", http=client) is False
