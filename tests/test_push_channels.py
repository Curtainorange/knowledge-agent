"""推送通道发送器测试（ADR-14 的「送到眼前」扩展）。

只测发送器本身（channels 模块）：toast 走 winotify（可注入替身）、wechat 走
Server酱（httpx.MockTransport 注入）；缺 key / 异常响应都降级为 False，不抛异常。
enqueue 的旁路分发行为见 tests/test_push.py。
"""
from __future__ import annotations

import sys
import types

import httpx

from app.push import channels


# ---------- toast ----------


def test_send_toast_degrades_without_winotify(monkeypatch):
    """没装 winotify 时静默降级 False，不抛异常。"""
    monkeypatch.setitem(sys.modules, "winotify", None)
    assert channels.send_toast("标题", "正文") is False


def test_send_toast_calls_notification(monkeypatch):
    calls: list[dict] = []

    class FakeNotification:
        def __init__(self, **kw):
            self.kw = kw

        def show(self):
            calls.append(self.kw)

    fake = types.ModuleType("winotify")
    fake.Notification = FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", fake)

    assert channels.send_toast("学习健康报告", "本周新增 3 条知识") is True
    assert calls and calls[0]["title"] == "学习健康报告"
    assert calls[0]["msg"] == "本周新增 3 条知识"


# ---------- wechat ----------


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
