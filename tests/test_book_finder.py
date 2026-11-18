"""全网找书测试（全 Mock，不触网、不真实调用模型）。

盯的是四件最容易出错的事：

1. **书名必须能指到检索结果**：编出来的书名、作者、出版年看起来和真的一样，
   用户照着去找才发现不存在——这比「没查到」更糟，所以无出处的候选必须被丢弃。
2. **搜不到要说出来**：`state` 必须区分「没搜到网页」「搜到了但没有对得上的」
   「判不出来」，不能用同一张卡糊过去。
3. **已看过的书不再重复**：用户说过「不是这本」，再找时它就不该再出现。
4. **一条都不入库**：候选只活在这次回合里，写库是用户点「记入知识库」之后的事。
"""
from __future__ import annotations

import json

import pytest

from app.agent import book_finder
from app.api import deps
from app.books.catalog import ResourceEntry
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm import websearch
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.helpers import sign_in


class ScriptedProvider(LLMProvider):
    """按剧本回放：第一步给检索词，第二步给判定结论。"""

    def __init__(self, rows: list[str]) -> None:
        self.rows = list(rows)
        self.calls: list[list[dict]] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(list(messages))
        text = self.rows.pop(0) if self.rows else "{}"
        return Completion(
            text=text,
            prompt_tokens=1,
            completion_tokens=1,
            finish_reason="stop",
            model=model,
            reasoning=reasoning,
        )


def _gateway(rows: list[str]) -> tuple[ModelGateway, ScriptedProvider]:
    provider = ScriptedProvider(rows)
    return ModelGateway(provider=provider), provider


def _queries(*items: str) -> str:
    return json.dumps({"queries": list(items)}, ensure_ascii=False)


def _match(*books: dict, summary: str = "", question: str = "") -> str:
    return json.dumps(
        {"summary": summary, "books": list(books), "question": question}, ensure_ascii=False
    )


@pytest.fixture(autouse=True)
def _key_available(monkeypatch):
    """本机 .env 有真 Key、CI 没有——把「有没有 Key」钉死，两种环境跑同一套用例。"""
    monkeypatch.setattr(websearch, "available", lambda: True)


@pytest.fixture
def fake_search(monkeypatch):
    """替身搜索引擎：两条结果，记录被搜过的词。"""
    seen: list[str] = []

    def _search(query, *, count=5):
        seen.append(query)
        return {
            "ok": True,
            "error": "",
            "results": [
                {
                    "title": "鲁滨逊漂流记 - 豆瓣读书",
                    "url": "https://book.douban.com/subject/1000001/",
                    "site": "豆瓣",
                    "date": "",
                    "summary": "笛福 1719 年小说，主人公海难后独自在荒岛生活二十八年，以日记记下经历。",
                },
                {
                    "title": "荒岛生存题材小说盘点",
                    "url": "https://example.com/island-novels",
                    "site": "example.com",
                    "date": "",
                    "summary": "一批以荒岛为舞台的小说。",
                },
            ],
        }

    monkeypatch.setattr(websearch, "search", _search)
    return seen


def test_found_book_carries_source_and_summary(session, fake_search):
    """正常路径：检索词来自模型，候选带出处链接，判定材料里看得见检索结果。"""
    gateway, provider = _gateway([
        _queries("荒岛 日记 小说", "海难 生存 小说"),
        _match(
            {
                "title": "《鲁滨逊漂流记》",
                "author": "笛福",
                "year": "1719",
                "why": "海难后独自在荒岛生活、用日记记下每天，与你记得的情节一致",
                "confidence": 0.86,
                "source_id": "S1",
            },
            summary="最可能是《鲁滨逊漂流记》。",
        ),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1",
        message="一个人海难后在荒岛上独自活下来，用日记记下每天——不记得书名",
    )

    assert result.state == "found"
    assert [b.title for b in result.books] == ["鲁滨逊漂流记"]  # 书名号已剥掉
    assert result.books[0].author == "笛福"
    assert result.books[0].url == "https://book.douban.com/subject/1000001/"
    assert result.books[0].confidence == 0.86
    assert "鲁滨逊" in result.summary
    assert fake_search == ["荒岛 日记 小说", "海难 生存 小说"]

    # 判定这一步必须真的拿到检索结果，否则候选还是凭记忆写的
    judged = "\n".join(str(m.get("content") or "") for m in provider.calls[-1])
    assert "[S1]" in judged
    assert "荒岛生活二十八年" in judged


def test_candidate_without_grounding_is_dropped(session, fake_search):
    """核心防编造：指不到任何一条检索结果的候选一律丢弃，宁可说没找到。"""
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match(
            {
                "title": "《孤岛余生录》",
                "author": "某位作家",
                "year": "2003",
                "why": "看起来很像",
                "confidence": 0.92,
                "source_id": "S9",  # 没有任何一条结果叫 S9
            },
            summary="应该是《孤岛余生录》。",
        ),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="一个人在海岛上独自生活"
    )

    assert result.books == []
    assert result.state == "not_found"
    assert "没有对得上" in result.note
    assert result.searched == 2


def test_missing_source_id_is_also_dropped(session, fake_search):
    """模型忘了写 source_id 与写错编号同样处理：没有证据就不给结论。"""
    gateway, _ = _gateway([
        _queries("荒岛 小说"),
        _match({"title": "《某个岛》", "why": "感觉像", "confidence": 0.7}),
    ])
    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="岛上生活的小说"
    )
    assert result.state == "not_found"
    assert result.books == []


def test_no_search_result_is_reported(session, monkeypatch):
    """搜了但一条网页都没有 → search_failed，不拿模型记忆顶上去。"""
    monkeypatch.setattr(
        websearch, "search", lambda query, count=5: {"ok": False, "results": [], "error": "检索被限流"}
    )
    gateway, provider = _gateway([_queries("荒岛 小说")])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="只记得在荒岛上"
    )

    assert result.state == "search_failed"
    assert result.books == []
    assert "没法判断" in result.note
    assert len(provider.calls) == 1, "没拿到材料就不该再让模型凭空判断"


def test_without_key_does_not_call_model(session, monkeypatch):
    """没配 Key 时停在原地：让模型凭记忆报书名正是要避免的事。"""
    monkeypatch.setattr(websearch, "available", lambda: False)
    gateway, provider = _gateway([])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="记得是本讲跑步的书"
    )

    assert result.state == "unavailable"
    assert "DEEPSEEK_API_KEY" in result.note
    assert provider.calls == []


def test_rejected_titles_do_not_come_back(session, fake_search):
    """用户说「不是这本」之后，再找时同一本不该再次出现在候选里。"""
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match(
            {"title": "《鲁滨逊漂流记》", "why": "还是它", "confidence": 0.8, "source_id": "S1"}
        ),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1",
        message="刚才那本不是，主角是女的",
        exclude=["鲁滨逊漂流记"],
    )

    assert result.books == []
    assert result.state == "not_found"


def test_unmatched_case_asks_one_question(session, fake_search):
    """一本都对不上时，模型给的问题要如实带出去——追问比硬给一本更有用。"""
    gateway, _ = _gateway([
        _queries("荒岛 小说"),
        _match(question="这本书大概是哪个年代写的？"),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="只记得跟岛有关"
    )

    assert result.state == "clarify"
    assert result.question == "这本书大概是哪个年代写的？"
    assert "没有对得上" in result.note


def test_confidence_words_are_tolerated(session, fake_search):
    """模型把把握度写成「高」是常态：一个展示用的数字不该让整次找书失败。"""
    gateway, _ = _gateway([
        _queries("荒岛 小说"),
        _match({"title": "《鲁滨逊漂流记》", "why": "对得上", "confidence": "高", "source_id": "s1"}),
    ])
    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="荒岛日记"
    )
    assert result.state == "found"
    assert result.books[0].confidence == 0.85


def test_broken_json_is_failed_not_not_found(session, fake_search):
    """判不出来 ≠ 没有这本书：模型输出破损时要单独报 failed。"""
    gateway, _ = _gateway([_queries("荒岛 小说"), "我不是 JSON，我是散文"])
    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="荒岛日记"
    )
    assert result.state == "failed"
    assert result.books == []


def test_query_planning_falls_back_to_user_words(session, monkeypatch):
    """第一步坏了不该让整条链路停摆：退到用户原话去搜，仍然能出结果。"""
    seen: list[str] = []

    def _search(query, *, count=5):
        seen.append(query)
        return {
            "ok": True,
            "error": "",
            "results": [
                {
                    "title": "当我谈跑步时我谈些什么",
                    "url": "https://book.douban.com/subject/1000002/",
                    "site": "豆瓣",
                    "date": "",
                    "summary": "村上春树关于跑步与写作的随笔集。",
                }
            ],
        }

    monkeypatch.setattr(websearch, "search", _search)
    gateway, _ = _gateway([
        "这里不是 JSON",
        _match(
            {"title": "当我谈跑步时我谈些什么", "author": "村上春树", "why": "跑步与写作", "confidence": 0.9, "source_id": "S1"}
        ),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1",
        message="村上春树写的那本讲跑步和写作的书",
    )

    assert result.state == "found"
    assert seen and "村上春树" in seen[0]


def test_nothing_is_written_to_the_knowledge_base(session, fake_search):
    """硬约定：找书不入库，留下的动作由用户自己点。"""
    before = len(KnowledgeRepository(session, user_id="u1").list_active("u1"))
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match({"title": "《鲁滨逊漂流记》", "why": "对得上", "confidence": 0.8, "source_id": "S1"}),
    ])
    book_finder.find_books(gateway=gateway, session=session, user_id="u1", message="荒岛日记")
    after = len(KnowledgeRepository(session, user_id="u1").list_active("u1"))
    assert before == after == 0


def test_found_book_carries_download_resource(session, fake_search, monkeypatch):
    """找到书名之后要能告诉用户「去哪拿」，否则他还得自己去搜资源。"""
    monkeypatch.setattr(
        book_finder.catalog,
        "lookup",
        lambda *a, **k: [
            ResourceEntry(
                title="鲁滨逊漂流记",
                author="笛福",
                link="https://url89.ctfile.com/f/31084289-1-abc?p=8866",
                categories=("经典",),
                formats=("epub", "mobi", "azw3"),
            )
        ],
    )
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match({"title": "《鲁滨逊漂流记》", "author": "笛福", "why": "对得上", "confidence": 0.8, "source_id": "S1"}),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="荒岛日记"
    )

    assert result.books[0].resource_url.startswith("https://url89.ctfile.com/")
    assert result.books[0].has_epub is True
    assert result.books[0].resource_category == "经典"


def test_resource_stays_empty_when_catalog_misses(session, fake_search, monkeypatch):
    """目录里没有这本书是常态（2.4 万条不可能包住所有书），不能因此改变找书结论。"""
    monkeypatch.setattr(book_finder.catalog, "lookup", lambda *a, **k: [])
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match({"title": "《鲁滨逊漂流记》", "why": "对得上", "confidence": 0.8, "source_id": "S1"}),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="荒岛日记"
    )

    assert result.state == "found"
    assert result.books[0].resource_url == ""
    assert result.books[0].has_epub is False


def test_mobi_only_resource_is_not_marked_readable(session, fake_search, monkeypatch):
    """只有 mobi/azw3 时链接照给，但 has_epub 必须为 False。

    上传白名单只认 .txt/.epub/.pdf；把只有 mobi 的条目说成「能读」，
    用户会下一份打不开的文件，然后以为阅读器坏了。
    """
    monkeypatch.setattr(
        book_finder.catalog,
        "lookup",
        lambda *a, **k: [
            ResourceEntry(title="鲁滨逊漂流记", link="https://url89.ctfile.com/f/x", formats=("mobi", "azw3"))
        ],
    )
    gateway, _ = _gateway([
        _queries("荒岛 日记 小说"),
        _match({"title": "《鲁滨逊漂流记》", "why": "对得上", "confidence": 0.8, "source_id": "S1"}),
    ])

    result = book_finder.find_books(
        gateway=gateway, session=session, user_id="u1", message="荒岛日记"
    )

    assert result.books[0].resource_url.endswith("/x")
    assert result.books[0].has_epub is False


def test_endpoint_returns_books_and_requires_auth(client, session, fake_search, monkeypatch):
    """接口层：结构与鉴权。前端只认这套字段，改字段要同步 mine.html。

    `client` 是**会话级**夹具，`app.dependency_overrides` 不会在用例之间自动清空；
    这里用 `monkeypatch.setitem` 登记覆盖，用例结束自动还原，否则后面所有用例都会
    拿到这个只回放两句话的假网关（实测会打挂 test_chat）。
    """
    user = sign_in(client, "book_search_api")
    monkeypatch.setitem(
        app.dependency_overrides,
        deps.get_gateway,
        lambda: _gateway([
            _queries("荒岛 日记 小说"),
            _match(
                {"title": "《鲁滨逊漂流记》", "author": "笛福", "why": "对得上", "confidence": 0.8, "source_id": "S1"},
                summary="最可能是这本。",
            ),
        ])[0],
    )
    # 资源库独立于模型：这里给一条确定的 epub，断言接口把它原样透出来。
    # 不 mock 的话，结果取决于 `data/book_catalog.json` 在不在、收录了哪一版 —— 那样测的是数据不是代码。
    monkeypatch.setattr(
        book_finder.catalog,
        "lookup",
        lambda *a, **k: [
            ResourceEntry(
                title="鲁滨逊漂流记",
                author="笛福",
                link="https://url89.ctfile.com/f/31084289-1-abc?p=8866",
                categories=("经典",),
                formats=("epub", "mobi", "azw3"),
            )
        ],
    )

    resp = client.post(
        "/api/v1/l1/book-search",
        json={"message": "一个人海难后在荒岛生活，用日记记下每天"},
        headers=user.headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "found"
    assert body["books"][0]["title"] == "鲁滨逊漂流记"
    assert body["books"][0]["url"].startswith("https://book.douban.com/")
    assert body["searched"] == 2
    assert body["summary"] == "最可能是这本。"
    assert body["request_id"]
    # 跨层契约：这两个字段由 mine.html 直接读取，缺一个按钮就不出现
    assert body["books"][0]["resource_url"].startswith("https://url89.ctfile.com/")
    assert body["books"][0]["has_epub"] is True

    anon = client.post("/api/v1/l1/book-search", json={"message": "找书"})
    assert anon.status_code == 401
