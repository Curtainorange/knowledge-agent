"""界面托管测试：根路由返回单页 HTML，且不抢占 /api 路由。"""
from __future__ import annotations

from tests.helpers import auth_headers


def test_index_serves_single_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "认知副驾" in body
    assert "/api/v1/l1/mine" in body  # 界面确实指向真实端点而非占位


def test_index_is_public(client):
    """登录页本身必须免鉴权，否则无法进入登录流程。"""
    assert client.get("/").status_code == 200


def test_api_routes_unaffected(client):
    assert client.get("/health").status_code == 200
    headers = auth_headers(client, "web_user")
    assert client.get("/api/v1/knowledge/items", headers=headers).status_code == 200
