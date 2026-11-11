"""贴链接即存（`linkgrab`）的测试。

**这个功能最容易做错的地方不是"能不能抓"，是"该不该抓"。** 「存这个链接」和
「问你关于这个链接的看法」只有语义能分开——「你觉得这篇怎么样（附链接）」比
「存一下」还长，任何字数/关键词规则都会把它判反。所以这里一半用例在守
「决定权在模型」，而不是守通路。

不触网：`websearch.fetch` 全部 monkeypatch；路由结论用预置的模型输出行驱动。
"""
from __future__ import annotations

import json

import pytest

from app.agent import linkgrab
from app.domain.models.knowledge_item import KnowledgeItem
from app.llm import websearch
from tests.helpers import auth_headers

ARTICLE = "https://example.com/posts/retrieval"
OTHER = "https://example.com/other"
LONG_BODY = "# 用 bge-m3 做中文检索\n\n" + "这是一段正文，讲混合检索怎么做。" * 40


# ---- 判定（只用于先验与兜底，不是主路径）------------------------------------


def test_url_only_accepts_bare_link_and_lead_verbs():
    assert linkgrab.is_url_only(ARTICLE)
    assert linkgrab.is_url_only(f"记一下：{ARTICLE}")
    assert linkgrab.is_url_only(f"存一下 {ARTICLE}。")


def test_url_only_rejects_questions_about_the_link():
    """问「这篇怎么样」不是在让我存它——这是最不能判反的一类。

    注意它比「存一下」还长：拿字数当判据必然翻车，所以主路径不靠这个函数。
    """
    assert not linkgrab.is_url_only(f"你觉得这篇怎么样 {ARTICLE}")
    assert not linkgrab.is_url_only(f"帮我看看 {ARTICLE} 讲了什么")
    assert not linkgrab.is_url_only(
        f"记一下：这篇文章不错 {ARTICLE} 但它的 benchmark 我觉得有问题，回头再核对"
    )


def test_extract_urls_dedupes_and_keeps_order():
    text = f"{ARTICLE} 还有 {OTHER} 以及 {ARTICLE}"
    assert linkgrab.extract_urls(text) == [ARTICLE, OTHER]


def test_extract_urls_ignores_bare_domains():
    """裸域名与普通英文无从区分，一概不认——误判一次就会去抓一个不存在的东西。"""
    assert linkgrab.extract_urls("看看 example.com 和 vue.js") == []


def test_user_note_keeps_what_the_user_actually_wrote():
    assert linkgrab.user_note(f"记一下：这篇 benchmark 有疑问 {ARTICLE}") == "这篇benchmark有疑问"
    # 整句只是命令 → 没有"用户写的内容"，不该往正文里塞
    assert linkgrab.user_note(f"存一下 {ARTICLE}") == ""
    assert linkgrab.user_note(ARTICLE) == ""


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


def test_save_url_keeps_user_words_alongside_the_page(session, fake_fetch):
    """用户自己写的判断不能被抓取冲掉——那句话往后最该被检索到。

    抓取只应补充内容。原话放在最前，读者一眼就知道「我为什么存这条」。
    """
    note = f"记一下：这篇 benchmark 我有疑问 {ARTICLE}"
    linkgrab.save_url(session=session, user_id="u_link", url=ARTICLE, note=note)

    session.flush()
    item = session.query(KnowledgeItem).one()
    assert item.raw_content.startswith(note)
    assert "混合检索" in item.raw_content


def test_save_url_does_not_prefix_a_pure_command(session, fake_fetch):
    """「存一下 <链接>」这种整句都是命令的，不该把命令本身写进正文。"""
    linkgrab.save_url(session=session, user_id="u_link", url=ARTICLE, note=f"存一下 {ARTICLE}")

    session.flush()
    item = session.query(KnowledgeItem).one()
    assert not item.raw_content.startswith("存一下")
    assert item.raw_content.startswith("# 用 bge-m3")


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


# ---- 对话入口（端到端，路由结论由预置的模型输出决定）-----------------------


def _send(client, headers, message: str) -> dict:
    resp = client.post("/api/v1/agent/chat", json={"message": message}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture()
def provider():
    """脚本供应商：预置一行路由结论，就按它执行（模拟"判得准的模型"）。"""
    from app.api import deps
    from app.llm.gateway import ModelGateway
    from app.main import app
    from tests.test_agent_chat import ScriptedProvider

    scripted = ScriptedProvider()

    def override_gateway():
        return ModelGateway(provider=scripted)

    app.dependency_overrides[deps.get_gateway] = override_gateway
    yield scripted
    app.dependency_overrides.pop(deps.get_gateway, None)


def _route(provider, *, args: dict, capability: str = "knowledge_add") -> None:
    provider.rows.append(json.dumps(
        {"capability": capability, "args": args, "confidence": 0.95}, ensure_ascii=False
    ))


def test_chat_saves_page_when_model_says_store_it(client, session, provider, fake_fetch):
    _route(provider, args={"url": ARTICLE, "title": "用 bge-m3 做中文检索",
                           "content": ARTICLE})
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


def test_chat_keeps_user_words_when_model_also_wants_the_page(
    client, session, provider, fake_fetch
):
    """模型判定「要存网页」时，用户那句评价也必须留在库里。"""
    text = f"记一下：这篇 benchmark 我有疑问 {ARTICLE}"
    _route(provider, args={"url": ARTICLE, "content": text})
    headers = auth_headers(client, "link_note")
    _send(client, headers, text)

    session.flush()
    item = session.query(KnowledgeItem).one()
    assert "benchmark 我有疑问" in item.raw_content
    assert "混合检索" in item.raw_content


def test_chat_without_url_arg_stores_the_text_and_never_fetches(
    client, session, provider, fake_fetch
):
    """模型没给 url = 它判断这次不是「存这个网页」→ 按原文存，绝不抓取。"""
    text = f"这句话我记一下 {ARTICLE}"
    _route(provider, args={"title": "记一下", "content": text})
    headers = auth_headers(client, "link_note_only")
    body = _send(client, headers, text)

    assert body["card"]["kind"] == "knowledge_created"
    assert fake_fetch["called"] == []
    session.flush()
    item = session.query(KnowledgeItem).one()
    assert ARTICLE in item.raw_content, "按原文存时网址应保留在正文里"


def test_chat_ignores_url_the_user_never_sent(client, session, provider, fake_fetch):
    """模型给了一个原话里没有的地址 → 不采用，退回按原文录。

    抓用户没给过的链接比不抓更糟：他以为存的是 A，库里却是 B。
    """
    text = "记一下：今天想到一个检索思路"
    _route(provider, args={"url": ARTICLE, "content": text})
    headers = auth_headers(client, "link_hallucinated")
    _send(client, headers, text)

    assert fake_fetch["called"] == []
    session.flush()
    item = session.query(KnowledgeItem).one()
    assert "检索思路" in item.raw_content


def test_chat_opinion_question_does_not_store_anything(client, session, provider, fake_fetch):
    """「你觉得这篇怎么样（附链接）」是提问，不是存链接——一个字都不该入库。"""
    text = f"你觉得这篇怎么样 {ARTICLE}"
    _route(provider, args={}, capability="chat")
    headers = auth_headers(client, "link_opinion")
    body = _send(client, headers, text)

    assert body["capability"] == "chat"
    assert fake_fetch["called"] == []
    session.flush()
    assert session.query(KnowledgeItem).count() == 0


def test_chat_link_fetch_failure_says_so_and_writes_nothing(client, session, provider, fake_fetch):
    """抓不到就说抓不到，绝不存一条只有网址的空条目。"""
    fake_fetch["ok"] = False
    _route(provider, args={"url": ARTICLE, "content": ARTICLE})
    headers = auth_headers(client, "link_fail")
    body = _send(client, headers, ARTICLE)

    assert body["card"]["kind"] == "notice"
    assert "没存下来" in body["card"]["title"]
    session.flush()
    assert session.query(KnowledgeItem).count() == 0


def test_chat_extra_links_are_reported_not_silently_dropped(client, session, provider, fake_fetch):
    """句子里还提到别的链接、但没算进「要存的」→ 说清楚，别让用户以为都存了。"""
    urls = [f"https://example.com/{i}" for i in range(5)]
    text = " ".join(urls)
    _route(provider, args={"url": urls, "content": text})
    headers = auth_headers(client, "link_many")
    body = _send(client, headers, text)

    assert body["card"]["kind"] == "knowledge_created"
    assert "已存 3 条" in body["reply"]
    assert "另外还提到 2 个链接" in body["reply"]
    session.flush()
    assert session.query(KnowledgeItem).count() == 3
