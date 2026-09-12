"""对话端到端测试：回复、会话持久化、多轮上下文、request_id 贯穿、成本落库。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.models.cost_log import CostLog
from app.domain.models.conversation import Conversation
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.user_repository import UserRepository
from tests.helpers import auth_headers


def _count_by_task(session: Session, task_type: str) -> int:
    return session.query(CostLog).filter(CostLog.task_type == task_type).count()


def test_chat_returns_reply_and_request_id(client, session):
    resp = client.post(
        "/api/v1/chat",
        json={"message": "你好，我想找一个关于拖延症的内容"},
        headers=auth_headers(client, "chat_basic"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"]
    assert body["request_id"]
    assert "Mock" in body["reply"] or "[mock" in body["reply"]

    # 会话落库且含来回两条消息
    conv = session.get(Conversation, body["conversation_id"])
    assert conv is not None
    assert len(conv.messages) == 2
    assert conv.messages[-1]["role"] == "assistant"

    # 成本埋点落库（本轮新增至少一条；精确计数在 test_gateway 按 request_id 断言）
    session.flush()
    before = _count_by_task(session, "multi_turn_dialogue")
    # 上一次调用已在本测试开头？此处仅验证至少记录了一条，避免跨测试共享内存库干扰
    assert before >= 1


def test_chat_multi_turn_reuses_conversation(client, session):
    headers = auth_headers(client, "chat_multi")
    first = client.post("/api/v1/chat", json={"message": "第一句"}, headers=headers).json()
    second = client.post(
        "/api/v1/chat",
        json={"conversation_id": first["conversation_id"], "message": "第二句"},
        headers=headers,
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    conv = session.get(Conversation, first["conversation_id"])
    # 两次交互 → 4 条消息（user/assistant × 2），说明上下文延续而非新会话
    assert len(conv.messages) == 4
    assert conv.messages[0]["content"] == "第一句"
    assert conv.messages[2]["content"] == "第二句"


def test_user_repo_enforces_scope(client, session):
    # 结构性防越权：绑定 userA 后读取 userB 的会话应被拒
    repo_a = ConversationRepository(session, user_id="userA")
    conv = repo_a.create(user_id="userA")
    session.commit()
    session.expunge(conv)

    repo_a2 = ConversationRepository(session, user_id="userA")
    assert repo_a2.get(conv.id) is not None  # 同用户可见

    repo_b = ConversationRepository(session, user_id="userB")
    try:
        repo_b.get(conv.id)
        raise AssertionError("跨用户读取应被仓储拒绝")
    except PermissionError:
        pass