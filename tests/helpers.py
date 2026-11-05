"""测试辅助：注册并登录，返回认证头与真实 user_id。

测试库为共享内存 SQLite，用例之间不清库，因此重名注册会返回 409；
此处忽略注册结果，以登录为准（用户名按用例隔离使用）。

注意：鉴权落地后 user_id 由服务端签发（UUID），不再是调用方自选的字符串，
需要直接查库断言归属的用例必须用 `sign_in(...).user_id`，不能用用户名代替。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

DEFAULT_PASSWORD = "pw12345678"

_ROUTE_MARK = "用户这句话是："
_ROUTE_TAIL = "请输出路由 JSON"


def looks_like_route(text: str) -> bool:
    """这一行是不是一条预设的路由结论（含 capability 字段的 JSON）。"""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and "capability" in data


def business_calls(task_types: list[str]) -> list[str]:
    """去掉分流调用后的任务类型序列。

    分流现在是每轮必经的一次调用，而多数端到端用例关心的是**业务层调了几次模型**。
    直接断言 `task_types == [...]` 会把分流也算进去，读起来像是能力多调了一次。
    """
    return [name for name in task_types if name != "capability_routing"]


def route_stub(messages: list[dict]) -> str:
    """模拟「判得准的模型」，给端到端测试用。

    分流现在是**每轮都会调模型**的链路。端到端用例关心的是能力本身的行为
    （谁写了消息、卡片对不对、有没有副作用），不是分流质量——分流有专门用例。
    如果这些测试也真调模型，它们预置的脚本行会被吃掉一格，后面的能力就会拿到
    错位的返回，失败信息还指向完全无关的地方。

    所以这里按本地先验回一份合法路由 JSON：脚本行一格不动，能力测试只测能力。
    """
    text = ""
    for message in messages:
        content = str(message.get("content") or "")
        if _ROUTE_MARK in content:
            text = content.split(_ROUTE_MARK, 1)[1].split(_ROUTE_TAIL, 1)[0].strip()
            break

    from app.agent.router import _match_local

    route = _match_local(text)
    if route is None:
        return json.dumps({"capability": "chat", "args": {}, "confidence": 0.5}, ensure_ascii=False)
    return json.dumps(
        {
            "capability": route.capability,
            "args": route.args,
            "confidence": 1.0,
            "reason": "test stub",
        },
        ensure_ascii=False,
    )


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
