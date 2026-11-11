"""推送通道发送器测试（ADR-14 的「送到眼前」扩展）。

只测发送器本身（channels 模块）：toast 走自写 PowerShell 脚本（monkeypatch subprocess），
wechat 走 Server酱（httpx.MockTransport 注入）；缺 key / 异常响应 / 非零返回都降级 False。
enqueue 的旁路分发行为见 tests/test_push.py。
"""
from __future__ import annotations

import base64
import os

import httpx

from app.push import channels


def test_toast_app_id_is_ascii():
    """app_id 是 Windows AUMID，不支持非 ASCII：中文会让通知静默失效（11-11 踩坑）。"""
    assert channels.TOAST_APP_ID.isascii()


# ---------- toast ----------


def test_send_toast_writes_script_and_runs_powershell(monkeypatch, tmp_path):
    ps1 = tmp_path / "t.ps1"
    fd = os.open(str(ps1), os.O_CREAT | os.O_WRONLY)
    monkeypatch.setattr(channels.tempfile, "mkstemp", lambda **kw: (fd, str(ps1)))

    seen: dict = {}

    class _Proc:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["script"] = ps1.read_text(encoding="utf-8-sig")
        return _Proc()

    monkeypatch.setattr(channels.subprocess, "run", fake_run)

    assert channels.send_toast("标题", "正文") is True
    assert seen["cmd"][0] == "powershell.exe"
    assert "-File" in seen["cmd"] and str(ps1) in seen["cmd"]
    assert "ToastGeneric" in seen["script"]
    assert base64.b64encode("标题".encode("utf-8")).decode("ascii") in seen["script"]


def test_send_toast_returns_false_on_nonzero_rc(monkeypatch, tmp_path):
    ps1 = tmp_path / "t.ps1"
    fd = os.open(str(ps1), os.O_CREAT | os.O_WRONLY)
    monkeypatch.setattr(channels.tempfile, "mkstemp", lambda **kw: (fd, str(ps1)))

    class _Proc:
        returncode = 1
        stderr = b"some error"

    monkeypatch.setattr(channels.subprocess, "run", lambda cmd, **kw: _Proc())
    assert channels.send_toast("t", "b") is False


def test_send_toast_returns_false_on_exception(monkeypatch):
    def boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(channels.tempfile, "mkstemp", boom)
    assert channels.send_toast("t", "b") is False


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
