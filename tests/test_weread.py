"""微信读书同步测试。

客户端用 `httpx.MockTransport` 校验请求规范与错误映射；同步服务与端点用假 client，
**全程不触网、不依赖真实 API Key**。
"""
from __future__ import annotations

import json

import httpx
import pytest

from tests.helpers import auth_headers

GATEWAY = "https://gateway.test/api/agent/gateway"


def _make_client(handler, api_key: str = "wrk-test-key"):
    """注入 MockTransport 的客户端。"""
    from app.weread.client import WeReadClient

    return WeReadClient(
        api_key,
        gateway_url=GATEWAY,
        skill_version="1.0.4",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _noop_handler(payload=None):
    return lambda request: httpx.Response(200, json=payload or {"errcode": 0})


# ---------- 客户端 ----------

def test_client_requires_api_key():
    """没配 Key 时抛专用异常，端点据此提示「先去配置」而不是「同步失败」。"""
    from app.weread.client import WeReadNotConfigured

    with pytest.raises(WeReadNotConfigured):
        _make_client(_noop_handler(), api_key="   ")


def test_call_sends_flat_params_with_version():
    """业务参数必须与 api_name / skill_version **平铺**在同一层（官方规范）。"""
    seen: dict = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"errcode": 0, "books": []})

    _make_client(handler).call("/user/notebooks", count=7)

    assert seen["auth"] == "Bearer wrk-test-key"
    assert seen["body"]["api_name"] == "/user/notebooks"
    assert seen["body"]["skill_version"] == "1.0.4"
    assert seen["body"]["count"] == 7
    assert "params" not in seen["body"]  # 包进 params 会被后端忽略


def test_notebooks_follows_cursor_pagination():
    """笔记本概览用 lastSort 游标翻页（不支持 offset/limit）。"""
    bodies: list[dict] = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"hasMore": 1, "books": [{"bookId": "b1", "sort": 999}]})
        return httpx.Response(200, json={"hasMore": 0, "books": [{"bookId": "b2", "sort": 111}]})

    books = _make_client(handler).notebooks()

    assert [b["bookId"] for b in books] == ["b1", "b2"]
    assert "lastSort" not in bodies[0]
    assert bodies[1]["lastSort"] == 999


def test_my_reviews_paginates_by_synckey():
    keys: list = []

    def handler(request):
        body = json.loads(request.content)
        keys.append(body.get("synckey"))
        if len(keys) == 1:
            return httpx.Response(200, json={"hasMore": 1, "synckey": 42, "reviews": [{"review": {"reviewId": "r1"}}]})
        return httpx.Response(200, json={"hasMore": 0, "reviews": [{"review": {"reviewId": "r2"}}]})

    reviews = _make_client(handler).my_reviews("b1")

    assert [r["review"]["reviewId"] for r in reviews] == ["r1", "r2"]
    assert keys == [None, 42]


def test_call_raises_readable_error_on_errcode():
    from app.weread.client import WeReadError

    client = _make_client(lambda request: httpx.Response(200, json={"errcode": 1001, "errmsg": "参数缺失"}))
    with pytest.raises(WeReadError) as excinfo:
        client.call("/book/info", bookId="b1")
    assert "1001" in str(excinfo.value)
    assert "参数缺失" in str(excinfo.value)


def test_call_stops_on_upgrade_info():
    """官方要求：回包带 upgrade_info 必须停下提示升级，不能忽略继续。"""
    from app.weread.client import WeReadError

    client = _make_client(
        lambda request: httpx.Response(200, json={"errcode": 0, "upgrade_info": {"message": "请升级到 1.0.5"}})
    )
    with pytest.raises(WeReadError) as excinfo:
        client.call("/user/notebooks")
    assert "升级" in str(excinfo.value)


@pytest.mark.parametrize("status", [401, 429])
def test_call_maps_http_errors(status):
    from app.weread.client import WeReadError

    with pytest.raises(WeReadError):
        _make_client(lambda request: httpx.Response(status)).call("/user/notebooks")


# ---------- 同步服务 ----------

class _FakeClient:
    """假客户端：按 bookId 返回预置数据；fail_on 里的书模拟拉取失败。"""

    def __init__(self, notebooks, bookmarks=None, reviews=None, info=None, fail_on=None):
        self._notebooks = notebooks
        self._bookmarks = bookmarks or {}
        self._reviews = reviews or {}
        self._info = info or {}
        self._fail_on = set(fail_on or [])

    def notebooks(self):
        return list(self._notebooks)

    def bookmarks(self, book_id):
        if book_id in self._fail_on:
            from app.weread.client import WeReadError

            raise WeReadError(f"模拟失败 {book_id}")
        return self._bookmarks.get(book_id, {})

    def my_reviews(self, book_id):
        return self._reviews.get(book_id, [])

    def book_info(self, book_id):
        return self._info.get(book_id, {})


def _sample_data(book_id: str = "b1"):
    """一本书：1 条划线 + 1 条划线想法（带 deepLink 与章节）。"""
    notebooks = [{"bookId": book_id, "book": {"title": "刻意练习", "author": "作者甲"}, "sort": 100}]
    bookmarks = {
        book_id: {
            "updated": [{
                "bookmarkId": 9001, "bookId": book_id, "chapterUid": 3,
                "markText": "刻意练习的核心是心理表征", "createTime": 1700000000,
                "type": 1, "range": "100-120",
            }],
            "chapters": [{"chapterUid": 3, "chapterIdx": 2, "title": "第二章 心理表征"}],
            "book": {"title": "刻意练习"},
        }
    }
    reviews = {
        book_id: [{"review": {
            "reviewId": "r1", "content": "这解释了为什么刻意练习需要及时反馈",
            "abstract": "刻意练习的核心是心理表征", "range": "100-120",
            "chapterUid": 3, "chapterIdx": 2, "chapterName": "第二章 心理表征",
            "createTime": 1700000100,
        }}]
    }
    info = {
        book_id: {
            "bookId": book_id, "title": "刻意练习：如何从新手到大师",
            "author": "安德斯·艾利克森", "deepLink": "weread://reading?bId=b1",
        }
    }
    return notebooks, bookmarks, reviews, info


def _service(session, **overrides):
    from app.weread.service import WeReadSyncService

    notebooks, bookmarks, reviews, info = _sample_data()
    client = _FakeClient(notebooks, bookmarks, reviews, info, **overrides)
    return WeReadSyncService(session, client=client)


def _items(session, user_id: str):
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    return KnowledgeRepository(session, user_id=user_id).list_active(user_id)


def test_sync_creates_highlight_and_thought(session):
    """划线与想法各成一条知识，格式与「书籍摘录」同构。"""
    result = _service(session).sync(user_id="u_weread")

    assert result.created == 2
    assert result.total_books == 1
    assert result.scanned_books == 1
    assert result.pending_books == 0

    items = {item.source_item_id: item for item in _items(session, "u_weread")}
    assert set(items) == {"hl:9001", "rv:r1"}

    highlight = items["hl:9001"]
    assert highlight.source == "weread"
    assert highlight.title == "刻意练习：如何从新手到大师 · 第二章 心理表征"
    assert highlight.raw_content == "刻意练习的核心是心理表征"
    assert highlight.note == ""
    assert "划线" in highlight.tags
    assert highlight.source_locator["kind"] == "highlight"
    assert highlight.source_locator["chapter_index"] == 2
    assert highlight.source_locator["deep_link"] == "weread://reading?bId=b1"

    thought = items["rv:r1"]
    assert thought.raw_content == "刻意练习的核心是心理表征\n\n【我的想法】这解释了为什么刻意练习需要及时反馈"
    assert thought.note == "这解释了为什么刻意练习需要及时反馈"
    assert "读书笔记" in thought.tags
    assert thought.source_locator["kind"] == "review"


def test_sync_handles_nested_review_wrapper(session):
    """想法回包的层级不稳定：真实接口是两层（reviewId 在外、正文在内），文档写的是一层。

    两种都要能解析——否则真实同步会「一条想法都收不到」。
    """
    from app.weread.service import WeReadSyncService

    notebooks = [{"bookId": "b1", "book": {"title": "书"}, "sort": 1}]
    bookmarks = {"b1": {"updated": [], "chapters": []}}
    nested = {
        "idx": 1,
        "review": {
            "reviewId": "r_nested",
            "likesCount": 3,
            "review": {
                "reviewId": "r_nested",
                "content": "两层结构里的想法正文",
                "abstract": "两层结构对应的划线原文",
                "range": "10-20",
                "chapterUid": 7,
                "chapterIdx": 3,
                "chapterName": "第三章",
                "createTime": 1700000200,
            },
        },
    }
    flat = {"review": {"reviewId": "r_flat", "content": "一层结构里的想法正文"}}

    svc = WeReadSyncService(
        session,
        client=_FakeClient(notebooks, bookmarks, reviews={"b1": [nested, flat]}, info={}),
    )
    result = svc.sync(user_id="u_nested")

    assert result.created == 2
    items = {item.source_item_id: item for item in _items(session, "u_nested")}
    assert set(items) == {"rv:r_nested", "rv:r_flat"}

    nested_item = items["rv:r_nested"]
    assert nested_item.raw_content == "两层结构对应的划线原文\n\n【我的想法】两层结构里的想法正文"
    assert nested_item.note == "两层结构里的想法正文"
    assert nested_item.source_locator["chapter_index"] == 3
    assert nested_item.source_locator["chapter_title"] == "第三章"

    flat_item = items["rv:r_flat"]
    assert flat_item.raw_content == "一层结构里的想法正文"


def test_sync_is_idempotent(session):
    """重复同步只跳过，不产生重复条目（幂等键 = bookmarkId / reviewId）。"""
    svc = _service(session)
    first = svc.sync(user_id="u_once")
    second = svc.sync(user_id="u_once")

    assert first.created == 2
    assert second.created == 0
    assert second.skipped == 2
    assert len(_items(session, "u_once")) == 2


def test_sync_does_not_resurrect_deleted_items(session):
    """用户主动删掉的条目不该被下一次同步复活。"""
    from sqlalchemy import select

    from app.domain.models.knowledge_item import KnowledgeItem
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    user_id = "u_deleted"
    svc = _service(session)
    svc.sync(user_id=user_id)

    repo = KnowledgeRepository(session, user_id=user_id)
    for item in repo.list_active(user_id):
        repo.soft_delete(item.id)
    session.commit()

    again = svc.sync(user_id=user_id)
    assert again.created == 0
    assert again.skipped == 2
    assert repo.list_active(user_id) == []
    # 记录仍在库里（软删），只是不再参与列表与检索
    rows = list(session.scalars(select(KnowledgeItem).where(KnowledgeItem.user_id == user_id)))
    assert len(rows) == 2
    assert all(row.is_deleted for row in rows)


def test_sync_respects_item_cap(session):
    """单次同步有条数上限，未扫到的书留给下一次（前端提示「还有 N 本」）。"""
    from app.weread.service import WeReadSyncService

    notebooks = [
        {"bookId": "b1", "book": {"title": "书一"}, "sort": 100},
        {"bookId": "b2", "book": {"title": "书二"}, "sort": 99},
    ]
    bookmarks = {
        "b1": {"updated": [{"bookmarkId": 1, "markText": "书一的划线", "chapterUid": 1}], "chapters": []},
        "b2": {"updated": [{"bookmarkId": 2, "markText": "书二的划线", "chapterUid": 1}], "chapters": []},
    }
    svc = WeReadSyncService(
        session, client=_FakeClient(notebooks, bookmarks, reviews={}, info={})
    )
    result = svc.sync(user_id="u_cap", limit=1)

    assert result.created == 1
    assert result.scanned_books == 1
    assert result.total_books == 2
    assert result.pending_books == 1


def test_sync_continues_when_one_book_fails(session):
    """单本书拉取失败只计数并记日志，不影响其它书。"""
    from app.weread.service import WeReadSyncService

    notebooks = [
        {"bookId": "b1", "book": {"title": "书一"}, "sort": 100},
        {"bookId": "b2", "book": {"title": "书二"}, "sort": 99},
    ]
    bookmarks = {
        "b2": {"updated": [{"bookmarkId": 2, "markText": "书二的划线", "chapterUid": 1}], "chapters": []},
    }
    svc = WeReadSyncService(
        session, client=_FakeClient(notebooks, bookmarks, reviews={}, info={}, fail_on=["b1"])
    )
    result = svc.sync(user_id="u_fail")

    assert result.failed_books == 1
    assert result.created == 1
    assert [i.source_item_id for i in _items(session, "u_fail")] == ["hl:2"]


def test_sync_records_event_and_status(session):
    from app.domain.repositories.learning_event_repository import LearningEventRepository
    from app.feedback import events
    from app.weread.service import WeReadSyncService

    user_id = "u_status"
    _service(session).sync(user_id=user_id)

    rows = LearningEventRepository(session).list_recent(
        user_id, event_type=events.WEREAD_SYNCED, limit=5
    )
    assert len(rows) == 1
    assert rows[0].payload["created"] == 2

    status = WeReadSyncService(session).status(user_id=user_id)
    assert status["synced_items"] == 2
    assert status["last_synced_at"] is not None
    assert status["configured"] is False  # 测试环境刻意不配 Key


# ---------- 端点 ----------

def test_sync_endpoint_without_api_key_returns_400(client):
    """未配置 Key → 400 与可读提示（不是 500）。"""
    headers = auth_headers(client, "weread_nokey")
    resp = client.post("/api/v1/weread/sync", headers=headers)
    assert resp.status_code == 400
    assert "API Key" in resp.json()["detail"]


def test_status_endpoint_reports_zero_before_sync(client):
    headers = auth_headers(client, "weread_status_zero")
    body = client.get("/api/v1/weread/status", headers=headers).json()
    assert body["configured"] is False
    assert body["synced_items"] == 0
    assert body["last_synced_at"] is None


def test_sync_endpoint_writes_items(client, monkeypatch):
    """端点走完整链路：注入假 client → 入库 → 状态可查。"""
    from app.weread import service as weread_service

    notebooks, bookmarks, reviews, info = _sample_data()
    monkeypatch.setattr(
        weread_service,
        "build_client",
        lambda: _FakeClient(notebooks, bookmarks, reviews, info),
    )

    headers = auth_headers(client, "weread_endpoint_ok")
    resp = client.post("/api/v1/weread/sync", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["created"] == 2
    assert body["skipped"] == 0
    assert body["pending_books"] == 0

    status = client.get("/api/v1/weread/status", headers=headers).json()
    assert status["synced_items"] == 2
    assert status["last_synced_at"] is not None


def test_weread_endpoints_require_auth(client):
    assert client.get("/api/v1/weread/status").status_code == 401
    assert client.post("/api/v1/weread/sync").status_code == 401
