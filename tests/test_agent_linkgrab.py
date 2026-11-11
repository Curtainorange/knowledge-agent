"""贴链接即存（`linkgrab`）的测试。

**这个功能最容易做错的地方不是"能不能抓"，是"什么时候不该抓"。** 用户写
「记一下：这篇文章不错 https://x 但观点有问题」时，他要存的是自己那句话；
擅自抓取会把他的判断丢掉，库里多出一篇原文、少了他真正的笔记。所以这里
一半的用例在守边界，不只是守通路。

不触网：`websearch.fetch` 全部 monkeypatch。
"""
from __future__ import annotations

import pytest

from app.agent import linkgrab
from app.domain.models.knowledge_item import KnowledgeItem
from app.llm import websearch
from tests.helpers import auth_headers

ARTICLE = "https://example.com/posts/retrieval"
LONG_BODY = "# 用 bge-m3 做中文检索\n\n" + "这是一段正文，讲混合检索怎么做。" * 40


# ---- 判定：什么时候算「就是在让我存这个链接」--------------------------------


def test_url_only_accepts_bare_link_and_lead_verbs():
    assert linkgrab.is_url_only(ARTICLE)
    assert linkgrab.is_url_only(f"记一下：{ARTICLE}")
    assert linkgrab.is_url_only(f"存一下 {ARTICLE}。")
    # 一句短备注仍算「存链接」：备注是标签，不是要被保存的正文
    assert linkgrab.is_url_only(f"收藏一下 {ARTICLE} 讲检索的")


def test_url_only_rejects_text_that_happens_to_contain_a_link():
    """夹着自己写的字 → 不抓取。抓取会把他写的那句话丢掉。"""
    assert not linkgrab.is_url_only(
        f"记一下：这篇文章不错 {ARTICLE} 但它的 benchmark 我觉得有问题，回头再核对一遍"
    )
    assert not linkgrab.is_url_only("记一下：B+树更适合范围查询")


def test_extract_urls_dedupes_and_keeps_order():
    text = f"{ARTICLE} 还有 https://example.com/b 以及 {ARTICLE}"
    assert linkgrab.extract_urls(text) == [ARTICLE, "https://example.com/b"]


def test_extract_urls_ignores_bare_domains():
    """裸域名与普通英文无从区分，一概不认——误判一次就会去抓一个不存在的东西。"""
    assert linkgrab.extract_urls("看看 example.com 和 vue.js") == []


# ---- 抓取 + 入库 ------------------------------------------------------------


@pytest.fixture()
def fake_fetch(monkeypatch):
    """可控的抓取器：默认成功，`fail` 后返回失败。"""
    state = {"ok": True, "content": LONG_BODY, "title": "用 bge-m3 做中文检索", "called": []}

    def _fetch(url: str, *, max_chars: int = websearch.FETCH_CONTENT_CHARS) -> dict:
        state["called"].append({"url": url, "max_chars": max_chars})
        if not state["ok"]:
            return {"ok": False, "title": "", "content": "", "error": "这一页没能取到正文"}
        return {
            "ok": True,
            "title": state["title"],
            "content": state["content"][:max_chars],
            "error": "",
        }

    monkeypatch.setattr(websearch, "fetch", _fetch)
    return state


def test_save_url_stores_page_body_not_the_link(session, fake_fetch):
    """核心断言：库里躺着的是**正文**。存一个 URL 字符串等于什么都没存。"""
    result = linkgrab.save_url(session=session, user_id="u_link", url=ARTICLE)

    assert result["ok"] is True
    assert result["chars"] > 200
    session.flush()
    items = session.query(KnowledgeItem).all()
    assert len(items) == 1
    assert items[0].title == "用 bge-m3 做中文检索"
    assert "混合检索" in items[0].raw_content
    assert items[0].source == "manual"
    assert "联网资料" in (items[0].tags or [])


def test_save_url_uses_ingest_limit_not_the_prompt_limit(session, fake_fetch):
    """入库要传更长的上限：给模型看只要要点，存进库的是以后要检索全文的东西。"""
    linkgrab.save_url(session=session, user_id="u_link", url=ARTICLE)
    assert fake_fetch["called"][0]["max_chars"] == linkgrab.INGEST_CONTENT_CHARS
    assert linkgrab.INGEST_CONTENT_CHARS > websearch.FETCH_CONTENT_CHARS


def test_save_url_prefers_given_title(session, fake_fetch):
    """搜索结果里已经有标题时用它——那是用户刚看到的那个名字。"""
    result = linkgrab.save_url(
        session=session, user_id="u_link", url=ARTICLE, title_hint="我看到的那篇"
    )
    assert result["title"] == "我看到的那篇"


def test_save_url_failure_writes_nothing(session, fake_fetch):
    fake_fetch["ok"] = False
    result = linkgrab.save_url(session=session, user_id="u_link", url=ARTICLE)

    assert result["ok"] is False
    assert result["error"]
    session.flush()
    assert session.query(KnowledgeItem).count() == 0


# ---- 对话入口（端到端）-----------------------------------------------------


def _send(client, headers, message: str) -> dict:
    resp = client.post("/api/v1/agent/chat", json={"message": message}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _provider():
    """与 test_agent_chat 同构的脚本供应商；这里只贴链接，不产生业务模型调用。"""
    from tests.test_agent_chat import ScriptedProvider

    return ScriptedProvider()


@pytest.fixture()
def provider():
    from fastapi.testclient import TestClient  # noqa: F401 - 类型提示用

    from app.api import deps
    from app.llm.gateway import ModelGateway
    from app.main import app

    scripted = _provider()

    def override_gateway():
        return ModelGateway(provider=scripted)

    app.dependency_overrides[deps.get_gateway] = override_gateway
    yield scripted
    app.dependency_overrides.pop(deps.get_gateway, None)


def test_chat_bare_link_is_stored_as_body(client, session, provider, fake_fetch):
    headers = auth_headers(client, "link_chat")
    body = _send(client, headers, ARTICLE)

    assert body["capability"] == "knowledge_add"
    assert body["card"]["kind"] == "knowledge_created"
    assert body["card"]["title"] == "用 bge-m3 做中文检索"
    assert "正文约" in body["reply"]

    session.flush()
    items = session.query(KnowledgeItem).all()
    assert len(items) == 1
    assert "混合检索" in items[0].raw_content
    assert "对话录入" in (items[0].tags or [])


def test_chat_link_with_note_keeps_the_note(client, session, provider, fake_fetch):
    """夹带备注时不抓取：用户写的那句话才是他要存的东西。"""
    note = f"记一下：这篇文章不错 {ARTICLE} 但 benchmark 我觉得有问题"
    headers = auth_headers(client, "link_note")
    body = _send(client, headers, note)

    assert body["card"]["kind"] == "knowledge_created"
    session.flush()
    items = session.query(KnowledgeItem).all()
    assert len(items) == 1
    assert "benchmark 我觉得有问题" in items[0].raw_content
    assert fake_fetch["called"] == [], "夹带备注时不该抓取"


def test_chat_link_fetch_failure_says_so_and_writes_nothing(client, session, provider, fake_fetch):
    """抓不到就说抓不到，绝不存一条只有网址的空条目。"""
    fake_fetch["ok"] = False
    headers = auth_headers(client, "link_fail")
    body = _send(client, headers, ARTICLE)

    assert body["card"]["kind"] == "notice"
    assert "没存下来" in body["card"]["title"]
    session.flush()
    assert session.query(KnowledgeItem).count() == 0


def test_chat_too_many_links_is_reported_not_silently_dropped(client, session, provider, fake_fetch):
    """超过上限的链接必须说清楚没处理——静默丢弃等于骗用户「都存好了」。"""
    urls = " ".join(f"https://example.com/{i}" for i in range(5))
    headers = auth_headers(client, "link_many")
    body = _send(client, headers, urls)

    assert body["card"]["kind"] == "knowledge_created"
    assert "还有 2 个没处理" in body["reply"]
    session.flush()
    assert session.query(KnowledgeItem).count() == 3
