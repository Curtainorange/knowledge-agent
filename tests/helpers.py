"""测试辅助：注册并登录，返回认证头与真实 user_id。

测试库为共享内存 SQLite，用例之间不清库，因此重名注册会返回 409；
此处忽略注册结果，以登录为准（用户名按用例隔离使用）。

注意：鉴权落地后 user_id 由服务端签发（UUID），不再是调用方自选的字符串，
需要直接查库断言归属的用例必须用 `sign_in(...).user_id`，不能用用户名代替。
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PASSWORD = "pw12345678"


@dataclass
class AuthedUser:
    user_id: str
    username: str
    access_token: str
    refresh_token: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


def sign_in(client, username: str, password: str = DEFAULT_PASSWORD) -> AuthedUser:
    """注册（幂等）并登录，返回含真实 user_id 的凭证对象。"""
    client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, "nickname": username},
    )
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, f"登录失败：{resp.text}"
    body = resp.json()
    return AuthedUser(
        user_id=body["user_id"],
        username=body["username"],
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
    )


def auth_headers(client, username: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
    return sign_in(client, username, password).headers
