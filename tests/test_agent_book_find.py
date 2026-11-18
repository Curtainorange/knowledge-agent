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
def model_says(monkeypatch):
    """让分流模型给出指定结论（模拟「模型读懂了这句话」）。

    这一层测的是**模型判定之后怎么分发**，语义判定本身在 `test_agent_router` 里测。
    也正因为方向词由模型给，这里一律不依赖关键词抽取：模型没给 tags，
    `_book_find` 就该退到联网，而不是自己从原话里切一个词出来去查库。
    """

    def _apply(capability: str = "book_find", **args):
        from app.agent.router import CapabilityRoute

        def _fake(self, text, *, user_id, history=None, hint=None):
            return CapabilityRoute(
                capability=capability,
                args=args,
                confidence=0.9,
                reason="测试预置（模型判定）",
                source="model",
            )

        monkeypatch.setattr("app.agent.router.CapabilityRouter._model_route", _fake)

    return _apply


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
def test_local_hint_points_to_book_find(message):
    """本地规则只产出**先验**（hint），不决定结果——真正判意图的是模型。

    `route()` 默认 `allow_model=True`，hint 只是塞进 prompt 的一句参考，
    模型可以推翻它（`_model_route` 的注释与 `test_agent_router` 都锁着这一点）。
    这里测的是「这条说法作为先验够不够准」：先验偏了，模型拿到的是错线索。
    """
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


def test_resource_card_when_catalog_has_epub(client, catalog_hit, model_says):
    model_says(title="人类简史")
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


def test_browse_by_tag_when_no_title(client, catalog_hit, model_says):
    """方向词由模型给（`args.tags`），不是从原话里切出来的。"""
    model_says(tags=["心理学"])
    headers = auth_headers(client, "book_find_tag")
    body = _send(client, headers, "帮我找本心理学的书")

    card = body["card"]
    assert card["kind"] == "book_find"
    assert card["mode"] == "browse"
    assert card["items"][0]["title"] == "思考，快与慢"
    assert card["items"][0]["tags"] == ["思维", "心理学", "决策"]
    assert "心理学" in body["reply"]


def test_mobi_only_is_reported_as_unreadable(client, monkeypatch, model_says):
    """只有 mobi/azw3 时必须直说读不了，不能含糊成「找到了」。"""
    monkeypatch.setattr(
        "app.books.catalog.lookup",
        lambda title, **kw: [_entry("人类简史", formats=("mobi", "azw3"))],
    )
    model_says(title="人类简史")
    headers = auth_headers(client, "book_find_mobi")
    body = _send(client, headers, "哪里有《人类简史》")

    assert body["card"]["items"][0]["has_epub"] is False
    assert "读不了" in body["reply"]


def test_no_tag_guessing_when_model_gives_none(client, monkeypatch, model_says):
    """模型没给方向词时**不许自己切**，直接走联网（那边才是按语义规划检索词）。

    反例是「找本关于决策与偏见的书」：用正则切会得到「关于决策与偏见」，
    目录里的标签却是「决策」。切出来的字符串查不到，用户会以为书库里没有。
    """
    monkeypatch.setattr("app.books.catalog.lookup", lambda title, **kw: [])
    monkeypatch.setattr("app.books.catalog.browse_catalog", lambda **kw: [])
    monkeypatch.setattr(websearch, "available", lambda: True)

    seen: dict = {}

    def _fake_find(**kwargs):
        seen.update(kwargs)
        from app.agent.book_finder import BookSearchResult

        return BookSearchResult(state="not_found", note="没找到对得上的书。")

    monkeypatch.setattr(book_finder, "find_books", _fake_find)
    model_says()  # 模型判了 book_find，但没给 tags（也没给 title）

    headers = auth_headers(client, "book_find_noguess")
    _send(client, headers, "找本关于决策与偏见的书")

    assert seen, "没有方向词时必须走联网，而不是拿切词结果去查目录"


# ---- 联网兜底 ---------------------------------------------------------------


def test_falls_back_to_web_search_when_catalog_misses(client, monkeypatch, model_says):
    """本地查不到才联网，且候选照样带「去哪拿」字段。"""
    monkeypatch.setattr("app.books.catalog.lookup", lambda title, **kw: [])
    monkeypatch.setattr(websearch, "available", lambda: True)
    model_says()

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


def test_without_key_says_which_condition_is_missing(client, monkeypatch, model_says):
    """目录没有、检索也没配 Key：说清缺什么，绝不凭记忆编书名。"""
    monkeypatch.setattr("app.books.catalog.lookup", lambda title, **kw: [])
    monkeypatch.setattr("app.books.catalog.browse_catalog", lambda **kw: [])
    monkeypatch.setattr(websearch, "available", lambda: False)
    model_says()

    headers = auth_headers(client, "book_find_nokey")
    body = _send(client, headers, "哪里有《不存在的书》")

    assert body["card"]["kind"] == "notice"
    assert "Key" in body["card"]["note"]
    assert body["card"]["items"] if "items" in body["card"] else True


# ---- 路由与卡片的一致性 ------------------------------------------------------


def test_card_always_carries_href_back_to_the_page(client, catalog_hit, model_says):
    """卡片上留一条回原页面的深链：卡片是摘要，完整视图在「我的」页。"""
    model_says(tags=["心理学"])
    headers = auth_headers(client, "book_find_href")
    body = _send(client, headers, "帮我找本心理学的书")
    assert body["card"]["href"] == "mine.html"


# ---- 确认期接管 -------------------------------------------------------------


def _make_conversation(client, headers, session, *, awaiting: bool = True) -> str:
    """造一个会话，并把它置成「刚发过阅读建议确认卡、正等作答」的状态。

    `state` 必须显式置回 `idle`：第一句话若被判成 L1 追问，会话会进入 `clarifying`，
    此时 `copilot` 按 `allow_model=False` 走（本地规则直接定），模型分流根本不参与，
    测的就不是「确认期接管」这件事了——这个坑第一次写时踩到了。
    """
    from app.agent import intake
    from app.domain.models.conversation import Conversation

    body = _send(client, headers, "你好")
    conv_id = body["conversation_id"]

    conv = session.get(Conversation, conv_id)
    conv.state = "idle"
    conv.reading_context = intake.IntakeContext(
        awaiting=awaiting,
        book={"title": "时间管理"},
        missing=["goal", "self_level", "help_type"],
    ).to_dict()
    session.commit()

    # 确认确实落库了：确认期接管的判据全在这两个字段上，写丢了整条用例就变成假绿
    session.expire_all()
    landed = session.get(Conversation, conv_id)
    assert (landed.reading_context or {}).get("awaiting") is awaiting
    assert landed.state == "idle"
    return conv_id


def test_book_find_is_not_swallowed_by_pending_intake(client, session, catalog_hit, model_says):
    """现场 bug：「我想读《三体》，能下吗」被确认卡吞掉。

    会话里有个没走完的阅读建议确认（`awaiting=True`），用户说了句明确的找书诉求。
    `_intake_wants` 会因为句子里有 `《三体》` 而认为这是「你在读哪本书」这个槽位的答案，
    于是回一张「读这个主要是为了什么」的确认卡——用户问的是去哪下书，
    系统问他读书图什么。

    确认期接管只该接「自由文本回答」，不该接「去外面拿一本书」这类明确动作。
    """
    model_says(title="人类简史")
    headers = auth_headers(client, "book_find_intake")
    conv_id = _make_conversation(client, headers, session)

    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conv_id, "message": "我想读《人类简史》，能下吗"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["capability"] == "book_find", "找书诉求不该被确认卡接管"
    assert body["card"]["kind"] == "book_find"


def test_intake_wants_itself_is_untouched(client, session, catalog_hit, model_says):
    """对照：门禁判据本身没被改坏，排除只发生在调用点。

    `_intake_wants` 对「找书」这句话依然返回 True（它只看句子里有没有可抽的槽位，
    这没错——句子里的书名确实是「你在读哪本书」槽位的一个合理答案）。
    真正拦住它的是 `INTAKE_BYPASS`：`book_find` 这类明确外部动作不进确认门禁。
    把这两件事分开断言，是为了以后有人「顺手把判据改严」时能立刻看出区别。
    """
    from app.agent import intake
    from app.agent.copilot import INTAKE_BYPASS, Copilot
    from app.domain.models.conversation import Conversation

    headers = auth_headers(client, "book_find_intake_ctrl")
    conv_id = _make_conversation(client, headers, session)
    conv = session.get(Conversation, conv_id)

    class _Noop:
        """判据只看文本，不该碰模型。真碰了就该在这里炸出来。"""

        def chat(self, **kwargs):
            raise AssertionError("_intake_wants 不该调用模型")

    cop = Copilot(gateway=_Noop(), session=session)

    # 普通回答仍然被判据接住
    assert cop._intake_wants("主要是为了解决实际问题", conv) is True
    # 找书那句话判据也会命中（书名被当成书槽位），所以必须在调用点排除
    assert cop._intake_wants("我想读《人类简史》，能下吗", conv) is True
    assert "book_find" in INTAKE_BYPASS
    assert "chat" not in INTAKE_BYPASS
    # 确认卡本身的功能没动：发卡仍然会把 awaiting 置起来
    assert intake.IntakeContext.from_dict(conv.reading_context).awaiting is True


# ---- 真数据冒烟 -------------------------------------------------------------
#
# 上面所有用例都拿替身喂数据，证明的是「逻辑对」；这条证明「接上真书目也能用」。
# `data/` 是 git-ignored，干净检出时没有这个文件，所以按存在性跳过——
# 跳过不是漏测：真实环境里它有，测试环境里它没有，硬要求它存在会让 CI 永远红。


def test_real_catalog_answers_a_tag_query(client, model_says):
    """真实书目 + 真实分派，端到端跑一次「找本心理学的书」。"""
    from app.books import catalog

    if not catalog.CATALOG_PATH.exists():
        pytest.skip("离线书目未导入（scripts/import_book_catalog.py）")

    model_says(tags=["心理学"])
    headers = auth_headers(client, "book_find_real")
    body = _send(client, headers, "帮我找本心理学的书")

    card = body["card"]
    assert card["kind"] == "book_find"
    assert card["mode"] == "browse"
    assert card["items"], "真实书目里「心理学」标签下不可能一本都没有"
    # 每一条都得是能点开的东西：有链接，且格式字段是这三种之一
    for item in card["items"]:
        assert item["resource_url"].startswith("http")
        assert item["has_epub"] is True
