"""资料研究能力测试（全 Mock，不触网、不真实调用模型）。

这里盯的是四件容易出错的事：

1. **工具结果真的回灌给模型了**，而不是执行完就丢——否则模型第二轮的"建议"
   其实还是凭记忆写的，白花一次检索费。
2. **失败必须说出来**：搜不到却照样自信地给建议，用户没法从文字里分辨真假。
3. **预算真的封顶**：这条链路每次对话都可能触发，不封顶就是无底洞。
4. **一条都不许入库**：检索结果只是这次回合的答案，不是他的知识。
"""
from __future__ import annotations

import json

import pytest

from app.agent import research
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm import websearch
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider


class ScriptedProvider(LLMProvider):
    """按剧本回放：每一步可以只给 tool_calls，也可以直接给文本。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)
        self.calls: list[list[dict]] = []
        self.tools_seen: list[list | None] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        row = self.rows.pop(0) if self.rows else {"text": "没有了"}
        return Completion(
            text=row.get("text", ""),
            prompt_tokens=1,
            completion_tokens=1,
            finish_reason="stop",
            model=model,
            reasoning=reasoning,
            tool_calls=list(row.get("tool_calls") or []),
        )


def _gateway(rows: list[dict]) -> tuple[ModelGateway, ScriptedProvider]:
    provider = ScriptedProvider(rows)
    return ModelGateway(provider=provider), provider


def _search_call(query: str, call_id: str = "c1") -> dict:
    return {
        "text": "",
        "tool_calls": [
            {"id": call_id, "name": "web_search", "arguments": json.dumps({"query": query})}
        ],
    }


@pytest.fixture(autouse=True)
def _key_available(monkeypatch):
    """没 Key 时 `advise` 会直接返回失败（这是真实行为），但本文件测的是链路本身。

    本机 .env 里带真实 Key，而 CI 里没有——两种环境都要跑同一套用例，
    所以这里显式把「有没有 Key」这件事钉成 True。
    """
    monkeypatch.setattr(websearch, "available", lambda: True)


@pytest.fixture
def fake_search(monkeypatch):
    """替身搜索引擎。返回固定两条结果，并记录被搜过的词。"""
    seen: list[str] = []

    def _search(query, *, count=5):
        seen.append(query)
        return {
            "ok": True,
            "error": "",
            "results": [
                {
                    "title": "动手学深度学习",
                    "url": "https://zh.d2l.ai/",
                    "site": "d2l.ai",
                    "date": "",
                    "summary": "可运行的深度学习教材",
                },
                {
                    "title": "机器学习课程",
                    "url": "https://example.com/ml-course",
                    "site": "example.com",
                    "date": "",
                    "summary": "入门课程",
                },
            ],
        }

    monkeypatch.setattr(websearch, "search", _search)
    return seen


@pytest.fixture
def fake_fetch(monkeypatch):
    def _fetch(url):
        return {"ok": True, "title": "动手学深度学习", "content": "第一章 引言……", "error": ""}

    monkeypatch.setattr(websearch, "fetch", _fetch)


def test_search_results_are_fed_back_to_model(session, fake_search):
    """检索结果必须回到模型手里：第二轮 prompt 里要看得见真实标题与链接。"""
    gateway, provider = _gateway([
        _search_call("深度学习 教材"),
        {"text": "先读《动手学深度学习》，配合 d2l.ai 的免费课程。"},
    ])

    result = research.advise(
        gateway=gateway, session=session, user_id="u1",
        question="帮我找一些学人工智能的材料", topic="人工智能",
    )

    assert result.state == "ok"
    assert result.tool_calls == 1
    assert fake_search == ["深度学习 教材"]
    # 第二轮的消息里必须带上第一次检索的结果
    second = "\n".join(str(m.get("content") or "") for m in provider.calls[-1])
    assert "https://zh.d2l.ai/" in second
    assert "动手学深度学习" in second
    # 工具回灌要用 tool 角色并带上 tool_call_id，否则协议对不上
    tool_msgs = [m for m in provider.calls[-1] if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "c1"


def test_sources_are_collected_for_the_card(session, fake_search):
    gateway, _ = _gateway([_search_call("机器学习 课程"), {"text": "两家都不错。"}])
    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")
    assert [s["url"] for s in result.sources] == [
        "https://zh.d2l.ai/",
        "https://example.com/ml-course",
    ]
    assert result.queries == ["机器学习 课程"]


def test_nothing_is_written_to_the_knowledge_base(session, fake_search):
    """硬约定：检索结果只活在这次回合里，一条都不入库。"""
    before = len(KnowledgeRepository(session, user_id="u1").list_active("u1"))
    gateway, _ = _gateway([_search_call("AI 教材"), {"text": "推荐这几份。"}])
    research.advise(gateway=gateway, session=session, user_id="u1", question="找 AI 材料")
    after = len(KnowledgeRepository(session, user_id="u1").list_active("u1"))
    assert before == after == 0


def test_model_can_answer_without_searching(session):
    """模型判断不需要联网时，如实标成 no_search——不能让用户以为这是实时资料。"""
    gateway, provider = _gateway([{"text": "这个概念本身不难，先说清楚它是什么……"}])
    result = research.advise(
        gateway=gateway, session=session, user_id="u1", question="什么是梯度下降"
    )
    assert result.state == "no_search"
    assert result.tool_calls == 0
    assert "没有联网检索" in result.note
    assert provider.tools_seen[0] is not None  # 工具仍然给到，是模型自己选择不调


def test_search_failure_is_reported_not_hidden(session, monkeypatch):
    monkeypatch.setattr(
        websearch,
        "search",
        lambda query, count=5: {"ok": False, "results": [], "error": "检索被限流"},
    )
    gateway, _ = _gateway([_search_call("AI 教材"), {"text": "这几本都值得读。"}])

    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")

    assert result.state == "search_failed"
    assert result.sources == []
    assert "没有返回结果" in result.note


def test_budget_caps_tool_calls(session, fake_search):
    """模型一直想搜也搜不完：总调用次数封顶，且次数用完后明确告诉它该收敛。"""
    rows = [_search_call(f"query-{i}") for i in range(research.MAX_ROUNDS)]
    gateway, provider = _gateway(rows)

    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")

    assert result.tool_calls <= research.MAX_TOOL_CALLS
    assert len(provider.calls) <= research.MAX_ROUNDS + 1
    assert result.text, "预算用尽也必须给用户一句话，不能空手而归"


def test_bad_tool_arguments_do_not_crash(session):
    """模型给半个 JSON 是常态：这一条工具结果不可用即可，不该把整轮打成 500。"""
    gateway, _ = _gateway([
        {"text": "", "tool_calls": [{"id": "c1", "name": "web_search", "arguments": "{坏参数"}]},
        {"text": "没查到，先按我知道的说。"},
    ])
    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")
    assert result.text
    assert result.state in ("search_failed", "no_search")


def test_unknown_tool_is_ignored(session):
    """模型编一个没给它的工具名 → 不执行、也不报错，直接当它没调工具。"""
    gateway, _ = _gateway([
        {"text": "", "tool_calls": [{"id": "c1", "name": "read_local_file", "arguments": "{}"}]},
        {"text": "这件事我做不了。"},
    ])
    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")
    assert result.tool_calls == 0
    assert result.state == "no_search"


def test_fetch_adds_its_page_to_sources(session, fake_search, fake_fetch):
    """抓过正文的页面本身就是来源：即使它没出现在搜索结果里也得记上。"""
    gateway, _ = _gateway([
        {"text": "", "tool_calls": [
            {"id": "c1", "name": "web_fetch", "arguments": json.dumps({"url": "https://zh.d2l.ai/"})}
        ]},
        {"text": "这一页的目录适合按章推进。"},
    ])
    result = research.advise(gateway=gateway, session=session, user_id="u1", question="找材料")
    assert any(s["url"] == "https://zh.d2l.ai/" for s in result.sources)


def test_card_lists_only_the_sources_actually_cited():
    """卡片只列正文引用过的来源：检索回来十几条、大半没看，全铺上去像没挑过。"""
    found = {
        "a": {"title": "A", "url": "https://a.example/x", "site": ""},
        "b": {"title": "B", "url": "https://b.example/y?p=1", "site": ""},
    }
    picked = research._cited_sources("先看 https://a.example/x 这一份。", found)
    assert [s["url"] for s in picked] == ["https://a.example/x"]

    # 正文一个链接都没写 → 退回全量，至少让人能自己核对
    assert len(research._cited_sources("什么都不带。", found)) == 2


def test_reply_prefix_makes_degradation_visible():
    """降级时把话说在前面：一段自信的建议最容易被误当成「查过了」。"""
    from app.agent.cards import research_reply

    failed = research.ResearchResult(text="这几本都不错。", state="search_failed")
    assert research_reply(failed).startswith("这次没搜到可用的外部资料")

    normal = research.ResearchResult(text="这几本都不错。", state="ok")
    assert research_reply(normal) == "这几本都不错。"


def test_card_marks_external_and_keeps_links():
    """卡片要把出处摆出来、并说明没入库；没有链接的条目不该被渲染成可点。"""
    from app.agent.cards import research_card

    result = research.ResearchResult(
        text="推荐两份。",
        state="ok",
        topic="人工智能",
        sources=[{"title": "动手学深度学习", "url": "https://zh.d2l.ai/", "site": "d2l.ai"}],
    )
    card = research_card(key="k1", result=result)

    assert card["kind"] == "research"
    assert card["sources"][0]["url"] == "https://zh.d2l.ai/"
    assert "没有进你的知识库" in card["note"]
    assert card["sends"] and "人工智能" in card["sends"][0]["message"]
