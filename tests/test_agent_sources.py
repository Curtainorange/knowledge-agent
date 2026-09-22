"""外部阅读源与书架接进对话的测试（全 Mock，不触网）。

微信读书同步有两个容易被做错的地方，这份测试主要盯它们：

1. **没配 Key 时不能先发 pending 卡**。异步回合是「先占位、后台跑」，
   如果前提检查放在后台，用户会先看到「正在同步…」、等一个轮询周期之后
   才被告知「没配 Key」——比直接说清楚差得多。
2. **重复同步必须安全**。「继续同步剩下的 N 本」「再同步一次」这两个按钮
   都建立在幂等之上（`source_item_id` 去重，且软删的条目也算已存在）。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent import turns
from app.core.config import settings
from app.domain.models.book import Book
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.models.task_run import TaskRun
from app.main import app
from tests.helpers import auth_headers, sign_in


class FakeWeReadClient:
    """微信读书 gateway 的假件：返回两本有划线的书。

    与 `tests/test_weread.py` 的假件同款形态——真实客户端由 `httpx.MockTransport`
    在 HTTP 层打桩，这里更靠上一层，直接替掉 `build_client()`。
    """

    def __init__(self, books: int = 2) -> None:
        self._books = books

    def notebooks(self) -> list[dict]:
        return [
            {"bookId": f"b{i}", "book": {"title": f"第 {i} 本书", "author": "作者"}}
            for i in range(1, self._books + 1)
        ]

    def book_info(self, book_id: str) -> dict:
        return {"title": f"第 {book_id[-1]} 本书", "author": "作者", "deepLink": f"weread://{book_id}"}

    def bookmarks(self, book_id: str) -> dict:
        return {
            "chapters": [{"chapterUid": 1, "title": "第一章"}],
            "updated": [{
                "bookmarkId": f"{book_id}-m1",
                "markText": f"来自 {book_id} 的划线",
                "chapterUid": 1,
                "createTime": 1700000000,
            }],
        }

    def my_reviews(self, book_id: str) -> list[dict]:
        return []


@pytest.fixture()
def configured_weread(monkeypatch):
    """配好 Key + 假客户端。返回假客户端，便于按用例调整书数。"""
    monkeypatch.setattr(settings, "weread_api_key", "wrk-test")
    fake = FakeWeReadClient()

    import app.weread.service as weread_service

    monkeypatch.setattr(weread_service, "build_client", lambda: fake)
    return fake


def _send(client, headers, message, conversation_id=None):
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_worker(session, *, rounds: int = 3) -> None:
    from app.workers import runner

    for _ in range(rounds):
        if runner.run_once(session).claimed == 0:
            break


# ---- 前提检查 ---------------------------------------------------------------


def test_missing_key_is_reported_before_queueing(client, session, monkeypatch):
    """没配 Key：直接回提示卡，**不排队**。

    这是 preflight 存在的理由——把前提检查放在发卡之前，用户不用等一个轮询周期
    才知道自己缺的是一把 Key。
    """
    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "wd_nokey")

    body = _send(client, headers, "同步微信读书")

    assert body["capability"] == "weread_sync"
    assert body["card"]["kind"] == "notice"
    assert body["card"]["title"] == "还没配置微信读书的 API Key"
    assert body["card"]["href"] == "/books.html"
    session.flush()
    assert session.query(TaskRun).filter(TaskRun.task_name == turns.TASK_NAME).count() == 0


# ---- 同步本身 ---------------------------------------------------------------


def test_sync_creates_items_and_result_card(client, session, configured_weread):
    """没有 worker 时同步跑，结果直接成卡。"""
    headers = auth_headers(client, "wd_ok")

    body = _send(client, headers, "同步微信读书")

    card = body["card"]
    assert card["kind"] == "weread_sync"
    assert card["result"]["created"] == 2
    assert card["result"]["scanned_books"] == 2
    assert "新增 2 条知识" in body["reply"]

    session.flush()
    items = session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.source == "weread")
    ).all()
    assert len(items) == 2
    assert all(item.embed_status == "embedded" for item in items), "同步进来的条目必须已向量化"


def test_sync_is_queued_when_worker_enabled(client, session, configured_weread, monkeypatch):
    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "wd_async")

    body = _send(client, headers, "同步微信读书")

    assert body["card"]["kind"] == "pending"
    assert body["card"]["capability"] == "weread_sync"
    session.flush()
    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == turns.task_key(body["card"]["turn_id"]))
    ).first()
    assert row is not None and row.payload["capability"] == "weread_sync"


def test_repeat_sync_is_idempotent(client, session, configured_weread):
    """重复同步不重复入库——「再同步一次」这类按钮全都建立在这条之上。"""
    headers = auth_headers(client, "wd_twice")

    _send(client, headers, "同步微信读书")
    body = _send(client, headers, "同步微信读书")

    assert body["card"]["result"]["created"] == 0
    assert body["card"]["result"]["skipped"] == 2
    session.flush()
    assert session.query(KnowledgeItem).filter(KnowledgeItem.source == "weread").count() == 2


def test_leftover_books_offer_a_continue_button(client, session, configured_weread, monkeypatch):
    """单次有上限、还有书没扫完时，卡上要给「接着扫」的出口。

    否则「没同步完」看起来就像漏了数据，用户不知道该不该再点一次。
    """
    monkeypatch.setattr(settings, "weread_max_items_per_sync", 1)
    configured_weread._books = 2
    headers = auth_headers(client, "wd_pending")

    body = _send(client, headers, "同步微信读书")

    card = body["card"]
    assert card["result"]["pending_books"] == 1
    assert card["sends"] == [{"label": "继续同步剩下的 1 本", "message": "同步微信读书"}]
    assert "还有 1 本没扫完" in body["reply"]


def test_weread_is_wired_not_a_guide_card(client):
    """接入后不该再回引导卡——接线时漏改 WIRED 会表现成「点了没反应」。"""
    headers = auth_headers(client, "wd_wired")
    body = _send(client, headers, "同步微信读书")

    assert body["capability"] == "weread_sync"
    assert body["card"]["kind"] != "guide"


def test_sync_failure_degrades_to_a_failed_card(client, session, monkeypatch):
    """拉取失败要能说清楚，而不是留一张永远转圈的卡。"""
    monkeypatch.setattr(settings, "weread_api_key", "wrk-test")
    monkeypatch.setattr(settings, "worker_enabled", True)

    class BrokenClient(FakeWeReadClient):
        def notebooks(self):
            raise RuntimeError("gateway 502")

    import app.weread.service as weread_service

    monkeypatch.setattr(weread_service, "build_client", lambda: BrokenClient())
    headers = auth_headers(client, "wd_broken")

    body = _send(client, headers, "同步微信读书")
    assert body["card"]["kind"] == "pending"

    _run_worker(session)          # 失败 → 留在 pending 重试
    _run_worker(session)
    _run_worker(session)

    card = client.get(
        f"/api/v1/agent/conversation/{body['conversation_id']}", headers=headers
    ).json()["messages"][-1]["card"]
    assert card["kind"] == "failed", card
    assert "可以再试一次" in card["note"]


# ---- 书架 ------------------------------------------------------------------


def _seed_book(session, user_id: str, *, title: str, progress: float) -> str:
    from app.domain.repositories.book_repository import BookRepository

    book = BookRepository(session, user_id=user_id).create(
        user_id=user_id, title=title, author="某作者", format="epub",
        file_path="data/books/x.epub",
        chapters=[{"index": 0, "title": "第一章", "char_start": 0, "char_end": 100}],
        full_text="正文", total_chars=100,
    )
    book.read_progress = progress
    session.commit()
    return book.id


def test_books_lists_the_shelf_with_progress_and_reader_link(client, session):
    user = sign_in(client, "bk_list")
    book_id = _seed_book(session, user.user_id, title="剑来", progress=0.42)

    body = _send(client, user.headers, "我的书架里有什么")

    assert body["capability"] == "books"
    card = body["card"]
    assert card["kind"] == "books"
    assert card["books"][0]["title"] == "剑来"
    assert card["books"][0]["read_progress"] == 0.42
    # 阅读器的入口参数是 ?book=，写错就点不进去
    assert card["books"][0]["href"] == f"/reader.html?book={book_id}"
    assert card["books"][0]["chapter_count"] == 1
    assert "1 本在读" in body["reply"]


def test_empty_shelf_points_at_where_to_upload(client, session):
    """上传要选本地文件，对话里做不到——那就明说，并把人送到书架页。"""
    headers = auth_headers(client, "bk_empty")
    body = _send(client, headers, "我的书架里有什么")

    assert body["card"]["kind"] == "books"
    assert body["card"]["books"] == []
    assert "上传" in body["card"]["note"]
    assert body["reply"] and "书架" in body["reply"]


def test_books_is_synchronous(client, session, monkeypatch):
    """书架是纯本地查询，不该走后台——为一次列表查询绕一圈任务队列没有意义。"""
    monkeypatch.setattr(settings, "worker_enabled", True)
    user = sign_in(client, "bk_sync")
    _seed_book(session, user.user_id, title="剑来", progress=1.0)

    body = _send(client, user.headers, "我的书架里有什么")

    assert body["card"]["kind"] == "books"
    session.flush()
    assert session.query(TaskRun).filter(TaskRun.task_name == turns.TASK_NAME).count() == 0


def test_books_no_longer_a_guide_card(client, session):
    headers = auth_headers(client, "bk_wired")
    body = _send(client, headers, "我的书架里有什么")
    assert body["card"]["kind"] != "guide"
