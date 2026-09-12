"""鉴权测试：注册 / 登录 / 令牌校验 / 刷新 / 数据隔离 / 伪造请求头失效。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from tests.helpers import DEFAULT_PASSWORD, auth_headers


def test_register_returns_token_and_me_works(client):
    resp = client.post(
        "/api/v1/auth/register",
        json={"username": "auth_basic", "password": DEFAULT_PASSWORD, "nickname": "小明"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_minutes * 60
    assert body["username"] == "auth_basic"

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["username"] == "auth_basic"
    assert me.json()["nickname"] == "小明"


def test_register_duplicate_username_conflicts(client):
    payload = {"username": "auth_dup", "password": DEFAULT_PASSWORD}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    again = client.post("/api/v1/auth/register", json=payload)
    assert again.status_code == 409


def test_register_rejects_weak_or_malformed_input(client):
    short_pw = client.post(
        "/api/v1/auth/register", json={"username": "auth_short", "password": "123"}
    )
    assert short_pw.status_code == 422

    bad_name = client.post(
        "/api/v1/auth/register", json={"username": "有中文", "password": DEFAULT_PASSWORD}
    )
    assert bad_name.status_code == 422


def test_username_is_case_insensitive(client):
    client.post(
        "/api/v1/auth/register", json={"username": "Auth_Case", "password": DEFAULT_PASSWORD}
    )
    resp = client.post("/api/v1/auth/login", json={"username": "auth_case", "password": DEFAULT_PASSWORD})
    assert resp.status_code == 200


def test_login_wrong_password_and_unknown_user_are_indistinguishable(client):
    client.post("/api/v1/auth/register", json={"username": "auth_pw", "password": DEFAULT_PASSWORD})

    wrong = client.post("/api/v1/auth/login", json={"username": "auth_pw", "password": "wrong-password"})
    unknown = client.post("/api/v1/auth/login", json={"username": "auth_ghost", "password": "wrong-password"})

    assert wrong.status_code == unknown.status_code == 401
    # 两条路径的提示必须一致，否则可据此枚举账号是否存在
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_protected_endpoint_requires_token(client):
    for path in ("/api/v1/knowledge/items", "/api/v1/auth/me"):
        resp = client.get(path)
        assert resp.status_code == 401
        assert resp.headers.get("WWW-Authenticate") == "Bearer"


def test_forged_x_user_id_header_no_longer_works(client):
    """早期版本直接信任 X-User-Id，任何人改个头就能冒充他人；此测试锁定该缺口不复现。"""
    resp = client.get("/api/v1/knowledge/items", headers={"X-User-Id": "some-victim"})
    assert resp.status_code == 401


def test_malformed_authorization_header_rejected(client):
    for value in ("token-without-scheme", "Basic abc", "Bearer ", "Bearer  "):
        resp = client.get("/api/v1/knowledge/items", headers={"Authorization": value})
        assert resp.status_code == 401, value


def test_tampered_token_rejected(client):
    headers = auth_headers(client, "auth_tamper")
    token = headers["Authorization"].split(" ", 1)[1]
    forged = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    resp = client.get("/api/v1/knowledge/items", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_expired_token_rejected(client):
    payload = {
        "sub": "any-user",
        "typ": "access",
        "exp": int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp()),
    }
    expired = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    resp = client.get("/api/v1/knowledge/items", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert "过期" in resp.json()["detail"]


def test_refresh_token_cannot_access_business_api(client):
    """refresh 是长期凭证，若能直接访问业务接口就等于绕过了 access 的短期过期策略。"""
    body = client.post(
        "/api/v1/auth/register", json={"username": "auth_typ", "password": DEFAULT_PASSWORD}
    ).json()
    resp = client.get(
        "/api/v1/knowledge/items",
        headers={"Authorization": f"Bearer {body['refresh_token']}"},
    )
    assert resp.status_code == 401


def test_refresh_exchanges_for_new_access_token(client):
    body = client.post(
        "/api/v1/auth/register", json={"username": "auth_refresh", "password": DEFAULT_PASSWORD}
    ).json()

    resp = client.post("/api/v1/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert resp.status_code == 200
    new_access = resp.json()["access_token"]
    assert client.get(
        "/api/v1/knowledge/items", headers={"Authorization": f"Bearer {new_access}"}
    ).status_code == 200

    # access token 拿去换发应被拒（类型必须是 refresh）
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": new_access}).status_code == 401


def test_users_data_is_isolated(client):
    headers_a = auth_headers(client, "iso_user_a")
    headers_b = auth_headers(client, "iso_user_b")

    created = client.post(
        "/api/v1/knowledge/items",
        json={"title": "A 的私密条目", "content": "只有 A 能看到"},
        headers=headers_a,
    ).json()
    item_id = created["item_id"]

    assert client.get("/api/v1/knowledge/items", headers=headers_a).json()["total"] == 1
    assert client.get("/api/v1/knowledge/items", headers=headers_b).json()["total"] == 0
    assert client.get(f"/api/v1/knowledge/items/{item_id}", headers=headers_b).status_code == 403
