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
    assert "bubble.textContent =" in js
    assert "bubble.innerHTML" not in js


def test_send_is_mutually_exclusive():
    """一次只允许一轮在飞。

    不设这个闸门时，双击发送会并发两条请求，会话消息按返回顺序交错写库，
    历史里就会出现「答一、问二、答二、问一」这种错位。
    """
    js = _agent_js()
    assert "state.sending = true" in js
    assert "state.sending = false" in js
    assert "if (!message || state.sending)" in js


def test_workbench_has_no_capability_buttons():
    """工作台不再摆「能力快捷入口」按钮，改为由副驾主动开场。

    把「能做什么」摊成一排按钮，等于要用户先学会这个产品的功能分类、再挑一个点下去——
    那还是「点功能」，不是对话。所以这里反过来钉死：**不许**把快捷入口加回来，
    连它的取数接口也不该出现在前端。
    """
    js = _agent_js()
    assert "agent-chips" not in js
    # 查「函数定义」而不是名字本身：注释里提一句它的来历是有价值的，
    # 但那不构成把快捷入口加回来。真正要拦的是代码。
    assert "function loadChips" not in js
    assert "/api/v1/agent/capabilities" not in js


def test_workbench_opens_with_proactive_greeting():
    """进工作台只调一次 `/start`：新会话由副驾先开口，已有会话把历史读回来。

    这条同时锁住「恢复历史」与「开场」走**同一条路径**——分成两条的话，
    迟早出现「开场重复插入」或「刷新后历史丢卡片」，而且都不报错。
    """
    js = _agent_js()
    assert "/api/v1/agent/start" in js
    assert "function startConversation()" in js
    # 开场消息必须来自服务端返回值，前端不许自己编一句问候
    assert "state.messages = data.messages" in js


# ---------- 异步回合与操作回流的前端不变量 --------------------------------


def test_card_actions_use_event_delegation():
    """卡片内操作必须走事件委托。

    卡片会被反复重建（轮询到结果时、操作后重绘时），给按钮逐个绑监听的话，
    重建之后按钮就「点不动」了——而且这种失效只在特定时序下出现，极难复现。
    """
    js = _agent_js()
    assert "log.addEventListener('click'" in js
    assert "closest('[data-agent-action]')" in js


def test_send_buttons_share_the_delegation():
    """「按钮发消息」也要走同一套委托，并且真的复用 send()。

    这类按钮（生成周计划、按建议重排）本质是替用户说一句话，必须走完整的对话回合——
    自己拼一个请求就会绕过异步回合、结果落不到消息流里。
    """
    js = _agent_js()
    assert "closest('[data-agent-send]')" in js
    assert "send(sender.getAttribute('data-agent-send'))" in js


def test_polling_is_bounded():
    """轮询必须有上限。没有上限的话，一条卡死的 pending 消息会让页面永远打请求。"""
    js = _agent_js()
    assert "POLL_MAX" in js
    assert "state.polls" in js


def test_optimistic_message_is_rolled_back_on_failure():
    """乐观追加的用户消息在请求失败时要撤回。

    不撤的话界面里会留下一条「我说过、但服务端并不知道」的消息，
    下一轮对话的上下文与用户的认知就对不上了。
    """
    js = _agent_js()
    assert "state.messages.pop()" in js


def test_conversation_id_is_persisted_for_reload():
    """会话 id 落 localStorage：刷新页面才能把历史连卡片一起恢复。"""
    js = _agent_js()
    assert "cc_agent_conversation" in js
    assert "/api/v1/agent/conversation/" in js


def test_messages_array_is_the_single_source_of_truth():
    """DOM 由消息数组推导（`syncMessages`），不允许各路径各写各的 append。

    否则「DOM 说已忽略、轮询又把它变回待处理」这类不一致迟早出现。
    """
    js = _agent_js()
    assert "function syncMessages()" in js
    assert "function messageNode(" in js
    assert "function signature(" in js


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
