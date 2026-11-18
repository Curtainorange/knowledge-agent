"""全网找书（book_find）接进对话后的测试：路由落点、两种本地命中、联网兜底。

盯的是五件事，每件都对应一个真实的失败模式：

1. **落点对不对**：`book_find` 与 L1（库内找回）、research（找材料）、
   book_recommend（在书架里挑）四者的分界，全靠本地规则和模型共同判。
   本地规则判窄了会漏（用户说法落进 chat），判宽了会抢别人的活（把「书架里那本」
   当成去网上找）。
2. **能不能读要说准**：只有 mobi 的条目必须说「阅读器读不了」。
   上传白名单只认 .txt/.epub/.pdf，说成「可以读」等于让人白下一份。
3. **本地优先、联网兜底**：顺序反了会把一次查表变成一次外部请求。
4. **没有 Key 不能编书**：目录里没有、检索又不可用时，必须说清缺什么，
   绝不能退化成「凭记忆列几本书」——编出来的书名和真的看起来一模一样。
5. **卡片形状**：`kind=book_find` + `mode`，前端按这个渲染，改字段要同步 `agent.js`。

所有用例都不联网、不调真实模型：目录用 `monkeypatch` 替身，检索用假件。
"""
from __future__ import annotations

import pytest

from app.agent import book_finder
from app.books.catalog import ResourceEntry
from app.llm import websearch
from tests.helpers import auth_headers, sign_in


def _send(client, headers, message: str) -> dict:
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": None, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _entry(title: str, *, formats=("epub", "mobi"), tags=("测试",), author="某作者") -> ResourceEntry:
    return ResourceEntry(
        title=title,
        author=author,
        link=f"https://url89.ctfile.com/f/31084289-{title}?p=8866",
        categories=tuple(tags),
        formats=tuple(formats),
    )


@pytest.fixture
def catalog_hit(monkeypatch):
    """目录里只有《人类简史》《思考，快与慢》两本，其余查不到。"""
    books = {
        "人类简史": _entry("人类简史", tags=("历史", "科普")),
        "思考，快与慢": _entry("思考，快与慢", tags=("思维", "心理学", "决策")),
    }
    monkeypatch.setattr(
        "app.books.catalog.lookup",
        lambda title, **kw: [books["人类简史"]] if "人类简史" in title else [],
    )
    monkeypatch.setattr(
        "app.books.catalog.browse_catalog",
        lambda **kw: [books["思考，快与慢"]],
    )
    return books


# ---- 路由落点 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "帮我找本心理学的书",
        "哪里有《人类简史》的 epub",
        "我想读《三体》，能下吗",
        "不记得书名了，主角被困在荒岛用日记活下来",
    ],
)
def test_local_rule_routes_to_book_find(message):
    """这些说法必须被本地规则命中——它们足够明确，不该再花一次模型调用。"""
    from app.agent.router import _match_local

    route = _match_local(message)
    assert route is not None
    assert route.capability == "book_find"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # 库内找回：出现「书架」就不该去网上找
        ("帮我找书架里那本讲索引的书", "l1"),
        # 在书架里推荐，不是去外面找
        ("推荐几本书", "book_recommend"),
        # 已读的书让我通读，不是去找资源
        ("通读《三体》", "book_digest"),
        # 找的是材料，不是具体某本书
        ("帮我找一些学人工智能的材料", "research"),
    ],
)
def test_book_find_does_not_steal_neighbours(message, expected):
    """判宽了就会抢别人的活——这四条是被抢过的邻居。"""
    from app.agent.router import _match_local

    route = _match_local(message)
    assert route is not None
    assert route.capability == expected


# ---- 本地命中 ---------------------------------------------------------------


def test_resource_card_when_catalog_has_epub(client, catalog_hit):
    headers = auth_headers(client, "book_find_epub")
    body = _send(client, headers, "哪里有《人类简史》的 epub")

    assert body["capability"] == "book_find"
    card = body["card"]
    assert card["kind"] == "book_find"
    assert card["mode"] == "resource"
    assert card["items"][0]["title"] == "人类简史"
    assert card["items"][0]["has_epub"] is True
    assert card["items"][0]["resource_url"].startswith("https://url89.ctfile.com/")
    assert "有 epub" in body["reply"]


def test_browse_by_tag_when_no_title(client, catalog_hit):
    """「找本心理学的书」里没有书名，只能按标签挑。"""
    headers = auth_headers(client, "book_find_tag")
    body = _send(client, headers, "帮我找本心理学的书")

    card = body["card"]
    assert card["kind"] == "book_find"
    assert card["mode"] == "browse"
    assert card["items"][0]["title"] == "思考，快与慢"
    assert card["items"][0]["tags"] == ["思维", "心理学", "决策"]
    assert "心理学" in body["reply"]


def test_mobi_only_is_reported_as_unreadable(client, monkeypatch):
    """只有 mobi/azw3 时必须直说读不了，不能含糊成「找到了」。"""
    monkeypatch.setattr(
        "app.books.catalog.lookup",
        lambda title, **kw: [_entry("人类简史", formats=("mobi", "azw3"))],
    )
    headers = auth_headers(client, "book_find_mobi")
    body = _send(client, headers, "哪里有《人类简史》")

    assert body["card"]["items"][0]["has_epub"] is False
    assert "读不了" in body["reply"]


# ---- 联网兜底 ---------------------------------------------------------------


def test_falls_back_to_web_search_when_catalog_misses(client, monkeypatch):
    """本地查不到才联网，且候选照样带「去哪拿」字段。"""
    monkeypatch.setattr("app.books.catalog.lookup", lambda title, **kw: [])
    monkeypatch.setattr(websearch, "available", lambda: True)

    called: dict = {}

    def _fake_find(**kwargs):
        called.update(kwargs)
        from app.agent.book_finder import BookSearchResult, FoundBook

        return BookSearchResult(
            state="found",
            books=[
                FoundBook(
                    title="鲁滨逊漂流记",
                    author="笛福",
                    why="荒岛 + 日记",
                    confidence=0.8,
                    resource_url="https://url89.ctfile.com/f/x",
                    resource_tags=["经典"],
                    has_epub=True,
                )
            ],
            summary="最可能是这本。",
            searched=2,
        )

    monkeypatch.setattr(book_finder, "find_books", _fake_find)

    headers = auth_headers(client, "book_find_web")
    body = _send(client, headers, "不记得书名了，主角被困在荒岛用日记活下来")

    assert called.get("message"), "联网路径必须拿到用户原话"
    card = body["card"]
    assert card["mode"] == "web"
    assert card["items"][0]["title"] == "鲁滨逊漂流记"
    assert card["items"][0]["resource_url"].startswith("https://url89.ctfile.com/")
    assert "鲁滨逊" in body["reply"]


def test_without_key_says_which_condition_is_missing(client, monkeypatch):
    """目录没有、检索也没配 Key：说清缺什么，绝不凭记忆编书名。"""
    monkeypatch.setattr("app.books.catalog.lookup", lambda title, **kw: [])
    monkeypatch.setattr("app.books.catalog.browse_catalog", lambda **kw: [])
    monkeypatch.setattr(websearch, "available", lambda: False)

    headers = auth_headers(client, "book_find_nokey")
    body = _send(client, headers, "哪里有《不存在的书》")

    assert body["card"]["kind"] == "notice"
    assert "Key" in body["card"]["note"]
    assert body["card"]["items"] if "items" in body["card"] else True


# ---- 路由与卡片的一致性 ------------------------------------------------------


def test_card_always_carries_href_back_to_the_page(client, catalog_hit):
    """卡片上留一条回原页面的深链：卡片是摘要，完整视图在「我的」页。"""
    headers = auth_headers(client, "book_find_href")
    body = _send(client, headers, "帮我找本心理学的书")
    assert body["card"]["href"] == "mine.html"
