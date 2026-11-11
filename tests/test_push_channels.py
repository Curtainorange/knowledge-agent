"""推送通道发送器测试（ADR-14 的微信通道）。

只测 send_wechat（httpx.MockTransport 注入）：缺 key / 非法前缀 / 异常响应 / 网络错误
都降级为 False，不抛异常。enqueue 的旁路分发行为见 tests/test_push.py。
"""
from __future__ import annotations

import httpx

from app.push import channels


def test_send_wechat_no_key_returns_false():
    assert channels.send_wechat("t", "b", sendkey="") is False


def test_send_wechat_rejects_unknown_key_prefix():
    assert channels.send_wechat("t", "b", sendkey="not-a-key") is False


def test_send_wechat_posts_and_detects_success():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode("utf-8")
        return httpx.Response(200, json={"code": 0})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert channels.send_wechat("学习健康报告", "正文内容", sendkey="SCT123", http=client) is True
    assert "sctapi.ftqq.com" in seen["url"]
    assert "/SCT123.send" in seen["url"]


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
