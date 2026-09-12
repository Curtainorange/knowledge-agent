"""知识条目 CRUD 测试：列表 / 详情 / 更新（含 embedding 重算）/ 软删 / 越权 / 分页。

测试库为会话级共享内存 SQLite，不做跨用例清理，因此每个用例使用独立 user_id 隔离。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain.repositories.knowledge_repository import KnowledgeRepository
from tests.helpers import auth_headers, sign_in


def _headers(client, user: str) -> dict[str, str]:
    """注册并登录后返回认证头（各用例用独立用户名隔离数据）。"""
    return auth_headers(client, user)


def _create(
    client,
    user: str,
    title: str = "默认标题",
    content: str = "默认正文",
    headers: dict[str, str] | None = None,
) -> dict:
    resp = client.post(
        "/api/v1/knowledge/items",
        json={"title": title, "content": content},
        headers=headers or _headers(client, user),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _locate(session: Session, user_id: str, item_id: str):
    """按服务端签发的真实 user_id 取条目（用于直接查库断言归属）。"""
    return KnowledgeRepository(session, user_id=user_id).get(item_id)


def test_created_item_appears_in_list(client):
    created = _create(client, "u_list", title="数据库索引", content="B+树与哈希索引的区别")
    resp = client.get("/api/v1/knowledge/items", headers=_headers(client, "u_list"))
    assert resp.status_code == 200
    body = resp.json()

    assert body["total"] == 1
    assert body["limit"] == 20 and body["offset"] == 0
    item = body["items"][0]
    assert item["item_id"] == created["item_id"]
    assert item["title"] == "数据库索引"
    assert item["snippet"].startswith("B+树")
    assert item["embed_status"] == "embedded"
    assert "content" not in item  # 列表只给摘要，不回灌全文


def test_detail_returns_full_content(client):
    created = _create(client, "u_detail", title="长文", content="第一段\n第二段")
    resp = client.get(f"/api/v1/knowledge/items/{created['item_id']}", headers=_headers(client, "u_detail"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "第一段\n第二段"
    assert body["source"] == "manual"
    assert body["read_progress"] == 0.0


def test_update_text_recomputes_embedding(client, session):
    user = sign_in(client, "u_update")
    created = _create(client, "u_update", title="原标题", content="原正文", headers=user.headers)
    item_id = created["item_id"]
    before = _locate(session, user.user_id, item_id).embedding
    assert before is not None

    resp = client.patch(
        f"/api/v1/knowledge/items/{item_id}",
        json={"title": "新标题", "content": "全新正文内容"},
        headers=user.headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["updated_fields"] == ["content", "title"]
    assert body["embed_status"] == "embedded"

    after = _locate(session, user.user_id, item_id)
    assert after.title == "新标题"
    assert after.embedding != before  # 文本变了 → 向量重算


def test_update_progress_only_keeps_embedding(client, session):
    """只改进度不动文本时，不应触发无谓的向量重算。"""
    user = sign_in(client, "u_prog")
    created = _create(client, "u_prog", title="标题", content="正文", headers=user.headers)
    item_id = created["item_id"]
    before = _locate(session, user.user_id, item_id).embedding

    resp = client.patch(
        f"/api/v1/knowledge/items/{item_id}",
        json={"read_progress": 0.5},
        headers=user.headers,
    )
    assert resp.status_code == 200
    assert resp.json()["updated_fields"] == ["read_progress"]

    item = _locate(session, user.user_id, item_id)
    assert item.read_progress == 0.5
    assert item.embedding == before  # 文本未变 → 向量不动


def test_update_read_progress_rejects_out_of_range(client):
    created = _create(client, "u_bound")
    for bad in (1.5, -0.1):
        resp = client.patch(
            f"/api/v1/knowledge/items/{created['item_id']}",
            json={"read_progress": bad},
            headers=_headers(client, "u_bound"),
        )
        assert resp.status_code == 422


def test_update_requires_at_least_one_field(client):
    created = _create(client, "u_empty")
    resp = client.patch(
        f"/api/v1/knowledge/items/{created['item_id']}", json={}, headers=_headers(client, "u_empty")
    )
    assert resp.status_code == 400


def test_soft_delete_hides_from_list_but_keeps_row(client, session):
    user = sign_in(client, "u_delete")
    created = _create(client, "u_delete", title="待删除", content="内容", headers=user.headers)
    item_id = created["item_id"]

    resp = client.delete(f"/api/v1/knowledge/items/{item_id}", headers=user.headers)
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    body = client.get("/api/v1/knowledge/items", headers=user.headers).json()
    assert body["total"] == 0
    assert client.get(
        f"/api/v1/knowledge/items/{item_id}", headers=user.headers
    ).status_code == 404

    # 软删：行仍在、原文保留，仅置标记
    row = _locate(session, user.user_id, item_id)
    assert row is not None and row.is_deleted is True
    assert row.raw_content == "内容"


def test_cross_user_access_forbidden(client):
    """结构性防越权：userB 读/改/删 userA 的条目一律 403。"""
    created = _create(client, "userA", title="私密", content="不可见")
    item_id = created["item_id"]
    url = f"/api/v1/knowledge/items/{item_id}"

    assert client.get(url, headers=_headers(client, "userB")).status_code == 403
    assert client.patch(url, json={"title": "改了"}, headers=_headers(client, "userB")).status_code == 403
    assert client.delete(url, headers=_headers(client, "userB")).status_code == 403
    assert client.get("/api/v1/knowledge/items", headers=_headers(client, "userB")).json()["total"] == 0


def test_pagination_respects_limit_and_offset(client):
    for i in range(5):
        _create(client, "u_page", title=f"条目{i}", content=f"内容{i}")

    first = client.get("/api/v1/knowledge/items?limit=2&offset=0", headers=_headers(client, "u_page")).json()
    assert first["total"] == 5
    assert len(first["items"]) == 2

    second = client.get("/api/v1/knowledge/items?limit=2&offset=2", headers=_headers(client, "u_page")).json()
    assert len(second["items"]) == 2
    first_ids = {i["item_id"] for i in first["items"]}
    assert all(i["item_id"] not in first_ids for i in second["items"])


def test_missing_item_returns_404(client):
    assert client.get(
        "/api/v1/knowledge/items/not-exist", headers=_headers(client, "u_404")
    ).status_code == 404
