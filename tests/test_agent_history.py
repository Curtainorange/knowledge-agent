"""对话历史会话测试：列表 / 自动命名 / 重命名 / 删除（全 Mock，不触网）。

四条最值得钉死的不变量：

1. **自动命名 write-once**。首条用户消息派生标题，但用户重命名后永不覆盖——
   重命名是显式意图，被下一条消息冲掉等于「改了也白改」。
2. **列表对老数据要兜底**。`title` 列上线前一直是空串，列表必须现场派生，
   否则历史侧栏全是「新会话」，等于没有历史。
3. **越权语义与 GET /conversation 一致**。他人会话 403、不存在 404——
   列表/改名/删除三个入口不能成为越权的旁门。
4. **冲突检测与对话分开**。`kind` 现场派生（不加列），历史侧栏据此分段——
   跑过 l2 扫描的会话归 "l2"，其余归 "chat"。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.domain.models.conversation import Conversation
from app.domain.repositories.conversation_repository import (
    ConversationRepository,
    derive_kind,
    derive_title,
)
from tests.helpers import sign_in


def _send(client, headers, message, conversation_id=None):
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _list(client, headers, **params):
    resp = client.get("/api/v1/agent/conversations", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _repo(session, user_id) -> ConversationRepository:
    return ConversationRepository(session, user_id=user_id)


def test_list_is_empty_for_a_fresh_user(client):
    """新用户没有任何会话：items 为空、total 为 0。"""
    user = sign_in(client, "hist_empty")
    data = _list(client, user.headers)
    assert data["items"] == []
    assert data["total"] == 0


def test_list_orders_latest_first_and_carries_title_preview(client, session):
    """列表最近活跃在前，行内带标题、预览与消息数。"""
    user = sign_in(client, "hist_order")
    repo = _repo(session, user.user_id)

    first = repo.create(user_id=user.user_id)
    repo.append_message(first, "user", "第一个会话的消息")
    second = repo.create(user_id=user.user_id)
    repo.append_message(second, "user", "第二个会话的消息")
    # updated_at 只精确到秒，同秒时按 id 二级排序——手动拉开时间才能断言「最近在前」
    first.updated_at = datetime(2026, 1, 1, 12, 0, 0)
    second.updated_at = datetime(2026, 1, 2, 12, 0, 0)
    session.commit()

    data = _list(client, user.headers)
    assert [i["conversation_id"] for i in data["items"]] == [second.id, first.id]
    top = data["items"][0]
    assert top["title"] == "第二个会话的消息"
    assert top["preview"] == "第二个会话的消息"
    assert top["message_count"] == 1
    assert top["state"] == "idle"


def test_title_is_derived_from_first_user_message_only_once(client, session):
    """自动命名：换行压平、超长截断；后续消息不再改标题。"""
    user = sign_in(client, "hist_autoname")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    long_text = "这是一段很长的首条消息\n带着换行和    连续空白" + "，继续写下去直到超过四十个字的限制为止还要更长一些"
    repo.append_message(conv, "user", long_text)
    session.commit()

    assert "\n" not in conv.title
    assert "  " not in conv.title
    assert conv.title.startswith("这是一段很长的首条消息")
    assert len(conv.title) <= 41  # 40 字截断 + 省略号
    if len(derive_title(long_text)) > 40:
        assert conv.title.endswith("…")

    repo.append_message(conv, "user", "第二条消息不应该覆盖标题")
    session.commit()
    assert conv.title.startswith("这是一段很长的首条消息")


def test_rename_is_not_overwritten_by_later_messages(client, session):
    """用户重命名后，自动命名不得再覆盖。"""
    user = sign_in(client, "hist_rename_keep")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    repo.append_message(conv, "user", "最初的消息")
    session.commit()

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "我的自定义标题"},
        headers=user.headers,
    )
    assert resp.status_code == 200, resp.text

    session.refresh(conv)  # 请求与测试用不同 session，先刷掉本地缓存
    repo.append_message(conv, "user", "改名之后的消息")
    session.commit()
    assert conv.title == "我的自定义标题"


def test_list_falls_back_to_preview_when_title_is_empty(client, session):
    """老数据（title 空串）在列表里现场派生标题，不回写库。"""
    user = sign_in(client, "hist_legacy")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    repo.append_message(conv, "user", "老会话的第一句话")
    conv.title = ""  # 模拟自动命名上线前的存量行
    session.commit()

    data = _list(client, user.headers)
    assert data["items"][0]["title"] == "老会话的第一句话"
    session.refresh(conv)
    assert conv.title == ""  # 列表兜底是展示逻辑，不写库


def test_list_falls_back_to_new_conversation_label(client, session):
    """只有开场白的空会话：标题兜底为「新会话」。"""
    user = sign_in(client, "hist_blank_title")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    session.commit()

    data = _list(client, user.headers)
    assert data["items"][0]["title"] == "新会话"
    assert data["items"][0]["message_count"] == 0


def test_list_is_scoped_and_paginated(client, session):
    """列表按用户隔离，分页参数与 total 一致。"""
    alice = sign_in(client, "hist_scope_a")
    bob = sign_in(client, "hist_scope_b")
    repo_a = _repo(session, alice.user_id)
    a1 = repo_a.create(user_id=alice.user_id)
    repo_a.append_message(a1, "user", "alice 的旧会话")
    a2 = repo_a.create(user_id=alice.user_id)
    repo_a.append_message(a2, "user", "alice 的新会话")
    a1.updated_at = datetime(2026, 1, 1, 12, 0, 0)
    a2.updated_at = datetime(2026, 1, 2, 12, 0, 0)
    repo_b = _repo(session, bob.user_id)
    b1 = repo_b.create(user_id=bob.user_id)
    repo_b.append_message(b1, "user", "bob 的会话")
    session.commit()

    data = _list(client, alice.headers, limit=1, offset=1)
    assert data["total"] == 2
    assert len(data["items"]) == 1
    assert data["items"][0]["conversation_id"] == a1.id

    bob_data = _list(client, bob.headers)
    assert [i["conversation_id"] for i in bob_data["items"]] == [b1.id]
    assert all(i["title"].startswith("bob") for i in bob_data["items"])


def test_rename_updates_title(client, session):
    """重命名成功后回包与库内一致。"""
    user = sign_in(client, "hist_rename_ok")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    session.commit()

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "  B+树笔记  "},
        headers=user.headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "B+树笔记"  # 前后空白被剥掉
    session.refresh(conv)
    assert conv.title == "B+树笔记"


def test_rename_rejects_blank_and_too_long(client, session):
    """纯空格标题 422；超过 128 字 422。"""
    user = sign_in(client, "hist_rename_bad")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    session.commit()

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "   "},
        headers=user.headers,
    )
    assert resp.status_code == 422
    assert "标题" in resp.text

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "x" * 129},
        headers=user.headers,
    )
    assert resp.status_code == 422
    assert "标题" in resp.text


def test_rename_rejects_foreign_and_unknown(client, session):
    """改名：他人会话 403、不存在 404、无 token 401。"""
    owner = sign_in(client, "hist_rename_owner")
    stranger = sign_in(client, "hist_rename_stranger")
    repo = _repo(session, owner.user_id)
    conv = repo.create(user_id=owner.user_id)
    session.commit()

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "偷改标题"},
        headers=stranger.headers,
    )
    assert resp.status_code == 403

    resp = client.patch(
        "/api/v1/agent/conversations/no-such-id",
        json={"title": "新标题"},
        headers=owner.headers,
    )
    assert resp.status_code == 404

    resp = client.patch(
        f"/api/v1/agent/conversations/{conv.id}",
        json={"title": "新标题"},
    )
    assert resp.status_code == 401


def test_delete_removes_the_whole_conversation(client, session):
    """删除是硬删：行消失、消息消失，单会话读取 404。"""
    user = sign_in(client, "hist_delete_ok")
    repo = _repo(session, user.user_id)
    conv = repo.create(user_id=user.user_id)
    repo.append_message(conv, "user", "将要消失的对话")
    session.commit()

    resp = client.delete(f"/api/v1/agent/conversations/{conv.id}", headers=user.headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True

    resp = client.get(f"/api/v1/agent/conversation/{conv.id}", headers=user.headers)
    assert resp.status_code == 404
    # 用新 SELECT 查库（session.get 会命中本地身份映射里的陈旧对象）
    rows = session.execute(
        select(Conversation).where(Conversation.id == conv.id)
    ).scalars().all()
    assert rows == []

    data = _list(client, user.headers)
    assert data["items"] == []


def test_delete_rejects_foreign_and_unknown(client, session):
    """删除：他人会话 403、不存在 404、无 token 401。"""
    owner = sign_in(client, "hist_delete_owner")
    stranger = sign_in(client, "hist_delete_stranger")
    repo = _repo(session, owner.user_id)
    conv = repo.create(user_id=owner.user_id)
    session.commit()

    resp = client.delete(f"/api/v1/agent/conversations/{conv.id}", headers=stranger.headers)
    assert resp.status_code == 403

    resp = client.delete("/api/v1/agent/conversations/no-such-id", headers=owner.headers)
    assert resp.status_code == 404

    resp = client.delete(f"/api/v1/agent/conversations/{conv.id}")
    assert resp.status_code == 401


def test_first_chat_message_sets_the_title(client, session):
    """端到端：POST /agent/chat 的首条消息即写入标题（覆盖 Copilot 路径）。"""
    user = sign_in(client, "hist_e2e")
    result = _send(client, user.headers, "记一下：B+树更适合范围查询 #数据库")
    conversation_id = result["conversation_id"]

    data = _list(client, user.headers)
    row = next(i for i in data["items"] if i["conversation_id"] == conversation_id)
    assert row["title"].startswith("记一下：B+树更适合范围查询")


# ---- kind 派生：冲突检测与对话分开（历史侧栏分段）----------------------


def test_derive_kind_marks_conflict_detection_runs():
    """判定只认卡片协议：l2 结果卡 / l2 占位卡 → "l2"，其余 → "chat"。"""
    # 空会话、纯文本聊天都是对话
    assert derive_kind([]) == "chat"
    assert derive_kind(None) == "chat"
    assert derive_kind([{"role": "user", "content": "帮我检测冲突"}]) == "chat"
    # l2 扫描结果卡 → 冲突检测
    assert derive_kind([
        {"role": "user", "content": "帮我检测冲突"},
        {"role": "assistant", "content": "扫出 2 处", "card": {"kind": "l2_conflicts", "items": []}},
    ]) == "l2"
    # 进行中 / 失败的占位卡带 capability="l2"，同样算检测记录（否则失败的扫描
    # 会留在对话段里，「分开」就有了漏网的）
    assert derive_kind([
        {"role": "assistant", "content": "正在扫描冲突", "card": {"kind": "pending", "capability": "l2"}},
    ]) == "l2"
    assert derive_kind([
        {"role": "assistant", "content": "扫描失败", "card": {"kind": "failed", "capability": "l2"}},
    ]) == "l2"
    # 其它能力的占位卡不算冲突检测
    assert derive_kind([
        {"role": "assistant", "content": "同步中", "card": {"kind": "pending", "capability": "weread_sync"}},
    ]) == "chat"
    # 先聊后扫的混合会话带检测记录 → 归 "l2"
    assert derive_kind([
        {"role": "user", "content": "聊点别的"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "帮我检测冲突"},
        {"role": "assistant", "content": "扫出 1 处", "card": {"kind": "l2_conflicts", "items": []}},
    ]) == "l2"
    # 挖掘、同步这些非检测能力不改归类（要不要再细分等有需要再说）
    assert derive_kind([
        {"role": "user", "content": "咖啡馆", "source": "l1"},
        {"role": "assistant", "content": "想定位哪一段？", "source": "l1", "card": {"kind": "l1_clarify"}},
    ]) == "chat"


def test_list_exposes_kind_for_sidebar_grouping(client, session):
    """列表行带 kind：侧栏据此把冲突检测会话与普通对话分段。"""
    user = sign_in(client, "hist_kind")
    repo = _repo(session, user.user_id)

    chat_conv = repo.create(user_id=user.user_id)
    repo.append_message(chat_conv, "user", "普通聊天")
    l2_conv = repo.create(user_id=user.user_id)
    repo.append_message(l2_conv, "user", "帮我检测冲突")
    repo.append_message(
        l2_conv, "assistant", "扫出 2 处",
        card={"kind": "l2_conflicts", "items": [], "summary": {}},
    )
    session.commit()

    data = _list(client, user.headers)
    kinds = {i["conversation_id"]: i["kind"] for i in data["items"]}
    assert kinds[chat_conv.id] == "chat"
    assert kinds[l2_conv.id] == "l2"
