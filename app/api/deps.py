"""FastAPI 依赖注入：会话 / 模型网关 / 当前用户。

鉴权（见系统设计 9.3.1）：入口统一校验 `Authorization: Bearer <access token>`，
通过后把 `user_id` 注入请求上下文，下游端点只依赖 `user_id`，不重复认证。

校验不止验签名，还包括三件事（缺一项都会留下越权或「注销后仍能用」的缺口）：
1. 令牌类型必须是 access（refresh 不能直接访问业务接口）
2. 账号必须存在且未注销
3. 令牌版本必须等于用户当前版本（改密 / 注销后旧令牌立即失效）

注意：早期版本以 `X-User-Id` 请求头直接取用户，任何调用方都能改这个头冒充他人；
现已改为从签名令牌中解析，请求头不再具备决定身份的能力。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import InvalidTokenError, TokenExpiredError, decode_token_claims
from app.domain.db import SessionLocal
from app.domain.models.user import User
from app.domain.repositories.user_repository import UserRepository
from app.llm.gateway import ModelGateway

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_gateway() -> ModelGateway:
    # 应用级单例；provider 按 settings 选择（有 KEY→DeepSeek，否则 Mock）
    return ModelGateway()


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=_UNAUTHORIZED_HEADERS
    )


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise _unauthorized("缺少认证信息：请在 Authorization 头携带 Bearer token")
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise _unauthorized("认证头格式应为：Bearer <token>")
    return parts[1].strip()


def _authed_user(authorization: str | None, session: Session) -> User:
    """解令牌 → 校验账号状态与令牌版本 → 返回用户实体。所有鉴权分支的唯一出口。"""
    token = _extract_bearer(authorization)
    try:
        claims = decode_token_claims(token, expected_type="access")
    except TokenExpiredError as exc:
        raise _unauthorized("token 已过期，请刷新或重新登录") from exc
    except InvalidTokenError as exc:
        raise _unauthorized("token 无效") from exc

    user_id = str(claims["sub"])
    # 此处按 id 直查，故不绑定作用域（仓储的 user_id 过滤是给业务查询用的第二道防线）
    user = UserRepository(session).get(user_id)
    if user is None or user.is_deleted:
        raise _unauthorized("账号不存在或已注销")
    if int(claims.get("ver", 0)) != int(user.token_version):
        raise _unauthorized("登录状态已失效（密码已修改或账号已注销），请重新登录")
    return user


def get_user_id(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> str:
    """从 Bearer access token 解析出 user_id；无效 / 已注销 / 版本过期一律 401。"""
    return _authed_user(authorization, session).id


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    """取当前用户实体（与 get_user_id 同一套校验，FastAPI 会缓存依赖避免重复查询）。"""
    return _authed_user(authorization, session)


def get_user_id_flexible(
    authorization: str | None = Header(default=None),
    token: str | None = None,
    session: Session = Depends(get_session),
) -> str:
    """图片等「浏览器直接发起的资源请求」的鉴权。

    `<img src>` 由浏览器自己发出，**无法携带 Authorization 头**（token 存在
    localStorage，不在 cookie），若沿用 get_user_id 会一律 401、图片打不开。
    这里在 Header 缺失时允许用 `?token=<access token>` 兜底——校验逻辑与
    get_user_id 完全相同（同一出口 `_authed_user`），只是取值来源多了一条。
    """
    if not authorization and token:
        authorization = f"Bearer {token}"
    return _authed_user(authorization, session).id