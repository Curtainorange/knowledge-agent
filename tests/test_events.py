"""行为事件埋点测试（ADR-10）：写得到、不记正文、失败不反噬、跨用户隔离。

埋点是 L4/L5 的数据地基，因此这里重点验证「完整性」与「无害性」两件事：
完整性 = 各业务动作都真的落了一条事件；
无害性 = 埋点异常时业务照常成功，且事件表里不含正文。
"""
from __future__ import annotations

from sqlalchemy import select

from app.domain.models.learning_event import LearningEvent
from app.feedback import events
from tests.helpers import auth_headers


def _events(session, event_type: str | None = None) -> list[LearningEvent]:
    stmt = select(LearningEvent).order_by(LearningEvent.occurred_at.asc())
    if event_type:
        stmt = stmt.where(LearningEvent.event_type == event_type)
    return list(session.scalars(stmt))


# ---------- 完整性：各业务动作都落事件 ----------


def test_ingest_emits_created_event_without_content(session):
    from app.ingestion.service import IngestionService
    from app.retrieval.embedding import HashEmbedding

    svc = IngestionService(session, HashEmbedding())
    svc.add_knowledge(user_id="u1", title="数据库索引", content="B+树与哈希索引的区别")

    rows = _events(session, events.KNOWLEDGE_CREATED)
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["content_len"] == len("B+树与哈希索引的区别")
    assert "B+树" not in str(payload)  # 正文绝不进事件表
    assert "数据库索引" not in str(payload)


def test_update_and_delete_emit_events(session):
    from app.ingestion.service import IngestionService
    from app.retrieval.embedding import HashEmbedding

    svc = IngestionService(session, HashEmbedding())
    item = svc.add_knowledge(user_id="u1", title="标题", content="内容")

    svc.update_knowledge(user_id="u1", item_id=item.id, read_progress=0.5)
    rows = _events(session, events.KNOWLEDGE_UPDATED)
    assert len(rows) == 1
    assert rows[0].payload["text_changed"] is False  # 只改进度不算文本变更
    assert rows[0].payload["read_progress"] == 0.5

    svc.delete_knowledge(user_id="u1", item_id=item.id)
    deleted = _events(session, events.KNOWLEDGE_DELETED)
    assert len(deleted) == 1
    assert deleted[0].payload["found"] is True


def test_book_events_are_recorded(session):
    import zipfile

    from app.books.service import BookService

    svc = BookService(session)
    book = svc.upload(user_id="u1", filename="书.txt", content="第一章 起点\n正文。".encode("utf-8"))
    assert len(_events(session, events.BOOK_UPLOADED)) == 1

    svc.update_progress(user_id="u1", book_id=book.id, current_char=5, read_progress=0.3)
    progress = _events(session, events.BOOK_PROGRESS)
    assert len(progress) == 1
    assert progress[0].payload["read_progress"] == 0.3

    svc.add_note(user_id="u1", book_id=book.id, text="正文。", note="我的想法", chapter_index=1)
    notes = _events(session, events.NOTE_CREATED)
    assert len(notes) == 1
    assert notes[0].payload["has_thought"] is True


def test_capability_calls_emit_events(session):
    """L1 挖掘与 L2 扫描都要留痕——收敛轮数与冲突产出是需求验收口径的数据来源。"""
    import json

    from app.agent.l1_orchestrator import L1Orchestrator
    from app.agent.l2_orchestrator import L2Orchestrator
    from app.domain.repositories.knowledge_repository import KnowledgeRepository
    from app.llm.completion import Completion
    from app.llm.gateway import ModelGateway
    from app.llm.provider import LLMProvider

    repo = KnowledgeRepository(session, user_id="u1")
    item = repo.create(user_id="u1", title="数据库索引", content="B+树")
    session.commit()

    class FakeProvider(LLMProvider):
        def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
            text = (
                json.dumps({"claims": []}) if task_type == "batch_extraction"
                else json.dumps({"decision": "located", "item_ids": [item.id]})
            )
            return Completion(text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning)

    L1Orchestrator(ModelGateway(provider=FakeProvider()), session).mine(
        user_id="u1", conversation_id=None, message="索引"
    )
    l1_rows = _events(session, events.L1_MINE)
    assert len(l1_rows) == 1
    assert l1_rows[0].payload["state"] == "located"

    L2Orchestrator(ModelGateway(provider=FakeProvider()), session).scan(user_id="u1")
    assert len(_events(session, events.L2_SCAN)) == 1


def test_conflict_feedback_emits_event(client, session):
    from app.domain.repositories.conflict_repository import ConflictRepository

    headers = auth_headers(client, "event_feedback")
    from tests.helpers import sign_in

    user_id = sign_in(client, "event_feedback").user_id
    repo = ConflictRepository(session, user_id=user_id)
    row = repo.create(
        user_id=user_id, item_a_id="ia", item_b_id="ib",
        claim_a_id="ca", claim_b_id="cb", conflict_type="立场对立",
    )
    session.commit()

    resp = client.patch(
        f"/api/v1/l2/conflicts/{row.id}/state", json={"state": "ignored"}, headers=headers
    )
    assert resp.status_code == 200
    rows = [e for e in _events(session) if e.event_type == events.L2_CONFLICT_FEEDBACK]
    assert rows and rows[-1].payload["to_state"] == "ignored"


def test_login_emits_event(client, session):
    auth_headers(client, "event_login")  # 注册 + 登录
    rows = [e for e in _events(session) if e.event_type == events.AUTH_LOGIN]
    assert rows
    assert rows[-1].payload["username"] == "event_login"


# ---------- 无害性 ----------


def test_event_failure_does_not_break_main_flow(session, monkeypatch):
    """埋点抛异常时录入必须照样成功——旁路不能反噬主链路。"""
    from app.ingestion.service import IngestionService
    from app.retrieval.embedding import HashEmbedding

    class BrokenRepo:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("埋点表炸了")

    monkeypatch.setattr(events, "LearningEventRepository", BrokenRepo)
    svc = IngestionService(session, HashEmbedding())
    item = svc.add_knowledge(user_id="u1", title="照常录入", content="内容")
    assert item.id  # 主链路未受影响
    assert _events(session, events.KNOWLEDGE_CREATED) == []


# ---------- 查询接口 ----------


def test_events_endpoint_is_user_scoped(client, session):
    headers_a = auth_headers(client, "event_api_a")
    headers_b = auth_headers(client, "event_api_b")
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "A 的条目", "content": "内容"},
        headers=headers_a,
    )

    listed = client.get("/api/v1/events", headers=headers_a).json()
    assert listed["total"] >= 1
    assert listed["by_type"].get(events.KNOWLEDGE_CREATED, 0) >= 1
    assert all(e["event_type"] for e in listed["items"])

    # 另一个用户看不到 A 的事件
    other = client.get("/api/v1/events", headers=headers_b).json()
    assert all("A 的条目" not in str(e["payload"]) for e in other["items"])
    assert events.KNOWLEDGE_CREATED not in other["by_type"]

    filtered = client.get(f"/api/v1/events?event_type={events.KNOWLEDGE_CREATED}", headers=headers_a).json()
    assert all(e["event_type"] == events.KNOWLEDGE_CREATED for e in filtered["items"])


def test_events_endpoint_requires_auth(client):
    assert client.get("/api/v1/events").status_code == 401