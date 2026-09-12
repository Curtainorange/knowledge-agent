"""FastAPI 依赖注入：会话 / 模型网关 / 当前用户。

鉴权（见系统设计 9.3.1）：入口统一校验 `Authorization: Bearer <access token>`，
通过后把 `user_id` 注入请求上下文，下游端点只依赖 `user_id`，不重复认证。

注意：早期版本以 `X-User-Id` 请求头直接取用户，任何调用方都能改这个头冒充他人；
现已改为从签名令牌中解析，请求头不再具备决定身份的能力。
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import InvalidTokenError, TokenExpiredError, decode_token
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


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少认证信息：请在 Authorization 头携带 Bearer token",
            headers=_UNAUTHORIZED_HEADERS,
        )
    parts = authorization.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="认证头格式应为：Bearer <token>",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return parts[1].strip()


def get_user_id(authorization: str | None = Header(default=None)) -> str:
    """从 Bearer access token 解析出 user_id；无效一律 401。"""
    token = _extract_bearer(authorization)
    try:
        return decode_token(token, expected_type="access")
    except TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 已过期，请刷新或重新登录",
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 无效",
            headers=_UNAUTHORIZED_HEADERS,
        ) from exc


def get_current_user(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> User:
    """取当前用户实体（token 合法但账号已不存在时同样视为未认证）。"""
    user = UserRepository(session, user_id=user_id).get(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不存在或已注销",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return user
