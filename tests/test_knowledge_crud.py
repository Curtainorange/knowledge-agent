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


# ---------- 导出 ----------

def test_export_markdown(client):
    """导出 Markdown：取全量、带下载头、含标题与正文。"""
    headers = _headers(client, "u_export_md")
    _create(client, "u_export_md", title="导出一", content="正文一", headers=headers)
    _create(client, "u_export_md", title="导出二", content="正文二", headers=headers)

    resp = client.get("/api/v1/knowledge/export?format=markdown", headers=headers)
    assert resp.status_code == 200
    assert "text/markdown" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]

    body = resp.text
    assert "# 知识库导出" in body
    assert "共 2 条" in body
    assert "## 导出一" in body and "正文一" in body
    assert "## 导出二" in body and "正文二" in body


def test_export_json(client):
    """导出 JSON：结构完整、字段齐全。"""
    headers = _headers(client, "u_export_json")
    _create(client, "u_export_json", title="JSON 条目", content="JSON 正文", headers=headers)

    resp = client.get("/api/v1/knowledge/export?format=json", headers=headers)
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]

    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "JSON 条目"
    assert body["items"][0]["content"] == "JSON 正文"
    for key in ("item_id", "tags", "source", "created_at"):
        assert key in body["items"][0]


def test_export_is_user_scoped(client):
    """导出只能看到自己的条目，不会串号。"""
    _create(client, "u_export_a", title="A 的条目", content="A", headers=_headers(client, "u_export_a"))
    _create(client, "u_export_b", title="B 的条目", content="B", headers=_headers(client, "u_export_b"))

    body = client.get(
        "/api/v1/knowledge/export?format=json", headers=_headers(client, "u_export_a")
    ).json()
    titles = [i["title"] for i in body["items"]]
    assert "A 的条目" in titles
    assert "B 的条目" not in titles


def test_export_rejects_bad_format(client):
    """非法 format 应被 422 拦下（避免导出成意料之外的格式）。"""
    resp = client.get(
        "/api/v1/knowledge/export?format=pdf", headers=_headers(client, "u_export_bad")
    )
    assert resp.status_code == 422


# ---------- 搜索 ----------

def test_search_matches_title_and_content(client):
    """关键词命中标题或正文；无匹配返回空。"""
    headers = _headers(client, "u_search")
    _create(client, "u_search", title="数据库索引", content="B+树与哈希", headers=headers)
    _create(client, "u_search", title="另一篇", content="讲的是数据库事务", headers=headers)
    _create(client, "u_search", title="无关条目", content="前端渲染", headers=headers)

    by_title = client.get("/api/v1/knowledge/items?q=索引", headers=headers).json()
    assert by_title["total"] == 1
    assert by_title["items"][0]["title"] == "数据库索引"

    by_content = client.get("/api/v1/knowledge/items?q=数据库", headers=headers).json()
    assert by_content["total"] == 2  # 标题一条 + 正文一条

    none = client.get("/api/v1/knowledge/items?q=完全没有的词", headers=headers).json()
    assert none["total"] == 0
    assert none["items"] == []


def test_search_is_user_scoped(client):
    """搜索不会跨用户串号。"""
    _create(client, "u_search_a", title="A 的数据库笔记", content="x", headers=_headers(client, "u_search_a"))
    _create(client, "u_search_b", title="B 的数据库笔记", content="x", headers=_headers(client, "u_search_b"))

    body = client.get("/api/v1/knowledge/items?q=数据库", headers=_headers(client, "u_search_a")).json()
    assert body["total"] == 1
    assert body["items"][0]["title"] == "A 的数据库笔记"


def test_search_excludes_deleted(client):
    """已软删的条目不参与搜索。"""
    headers = _headers(client, "u_search_del")
    created = _create(client, "u_search_del", title="待删的搜索条目", content="x", headers=headers)
    client.delete("/api/v1/knowledge/items/" + created["item_id"], headers=headers)

    body = client.get("/api/v1/knowledge/items?q=待删的搜索条目", headers=headers).json()
    assert body["total"] == 0


def test_search_with_pagination(client):
    """搜索 + 分页组合：total 是命中总数，分页在命中集内切。"""
    headers = _headers(client, "u_search_page")
    for i in range(5):
        _create(client, "u_search_page", title=f"检索目标 {i}", content="同主题", headers=headers)

    first = client.get("/api/v1/knowledge/items?q=检索目标&limit=2&offset=0", headers=headers).json()
    assert first["total"] == 5
    assert len(first["items"]) == 2

    second = client.get("/api/v1/knowledge/items?q=检索目标&limit=2&offset=2", headers=headers).json()
    first_ids = {i["item_id"] for i in first["items"]}
    assert all(i["item_id"] not in first_ids for i in second["items"])
