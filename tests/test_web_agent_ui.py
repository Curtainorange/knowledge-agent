"""对话工作台前端测试：卡片渲染的转义不变量 + 运行时行为。

为什么单独有这一层：卡片里的标题、摘要、标签全部来自用户输入，最终由字符串拼接
变成 HTML。这类代码在最容易出漏洞的地方，而它**完全无法用 pytest 覆盖**——
后端返回的是结构化 JSON，断言看不到最终 HTML。所以这里用 Node 跑真实的
`web/assets/agent.js`（vm 里只给 CC.esc 和 window，不给 DOM），把转义钉死。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT_JS = ROOT / "web" / "assets" / "agent.js"
HARNESS = Path(__file__).resolve().parent / "js" / "agent_behavior.js"


def _agent_js() -> str:
    return AGENT_JS.read_text(encoding="utf-8")


# ---------- 源码层不变量（不需要 node，永远执行）-------------------------


def test_card_renderer_reuses_shared_escaping():
    """必须复用 app.js 的 CC.esc，不能自己造一个「兜底转义」。

    静默不转义比报错危险得多：一旦有人为了「先跑起来」加个空转义的 fallback，
    卡片里的用户数据就直接进 HTML，而且没有任何测试会红。
    """
    js = _agent_js()
    assert "esc = CC.esc" in js
    assert "escapeHtml" not in js


def test_user_message_does_not_use_innerhtml():
    """用户输入的那一行必须走 textContent——它是唯一完全未经过过滤的内容。"""
    js = _agent_js()
    assert "bubble.textContent = text" in js


def test_send_is_mutually_exclusive():
    """一次只允许一轮在飞。

    不设这个闸门时，双击发送会并发两条请求，会话消息按返回顺序交错写库，
    历史里就会出现「答一、问二、答二、问一」这种错位。
    """
    js = _agent_js()
    assert "var sending = false" in js
    assert "if (!message || sending)" in js


def test_quick_entries_come_from_server():
    """快捷入口的文案与「是否已接入」必须由服务端给。

    前端硬编码一份必然与路由规则漂移：示例话术改了、本地规则没同步，
    点按钮就变成一次模型调用甚至兜底成闲聊，而且是静默退化。
    """
    js = _agent_js()
    assert "/api/v1/agent/capabilities" in js
    assert "item.wired" in js


# ---------- 运行时行为（Node；环境无 node 时跳过）------------------------


def test_card_renderer_runtime():
    """跑真实的 agent.js：转义、未知卡片降级、候选过滤、数值渲染。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("环境没有 node，跳过前端卡片渲染验证")

    proc = subprocess.run(
        [node, str(HARNESS), str(AGENT_JS)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, f"卡片渲染验证失败：\n{output}"
    assert "全部通过" in proc.stdout
