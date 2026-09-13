"""推送频率偏好端点测试（UC-G-02 主动推送偏好管理）。"""
from __future__ import annotations

from tests.helpers import auth_headers


def test_default_frequency_is_weekly(client):
    headers = auth_headers(client, "pref_user")
    resp = client.get("/api/v1/preferences", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["push_frequency"] == "weekly"


def test_update_frequency(client):
    headers = auth_headers(client, "pref_update")
    resp = client.put("/api/v1/preferences", json={"push_frequency": "quiet"}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["push_frequency"] == "quiet"

    # 回读确认持久化
    got = client.get("/api/v1/preferences", headers=headers).json()
    assert got["push_frequency"] == "quiet"


def test_invalid_frequency_rejected(client):
    headers = auth_headers(client, "pref_bad")
    resp = client.put("/api/v1/preferences", json={"push_frequency": "hourly"}, headers=headers)
    assert resp.status_code == 422


def test_preferences_require_auth(client):
    assert client.get("/api/v1/preferences").status_code == 401
    assert client.put("/api/v1/preferences", json={"push_frequency": "daily"}).status_code == 401


def test_preferences_are_user_scoped(client):
    a = auth_headers(client, "pref_a")
    b = auth_headers(client, "pref_b")
    client.put("/api/v1/preferences", json={"push_frequency": "quiet"}, headers=a)
    # B 不受 A 影响，仍是默认 weekly
    assert client.get("/api/v1/preferences", headers=b).json()["push_frequency"] == "weekly"
