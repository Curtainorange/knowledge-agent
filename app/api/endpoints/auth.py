"""认证端点：注册 / 登录 / 刷新令牌 / 当前用户（见系统设计 9.3.1）。

设计要点：
- 登录失败**不区分**「用户不存在」与「密码错误」，且用户不存在时仍走一次哈希校验，
  避免通过响应内容或耗时差异枚举账号。
- refresh token 只能用于换发 access token，不能直接访问业务接口（decode 时校验 typ）。
- 密码明文只在请求体内短暂存在，不落库、不入日志。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_session
from app.core import security, trace
from app.domain.models.user import User
from app.domain.repositories.user_repository import UserRepository

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

MIN_PASSWORD_LEN = 8
_USERNAME_PATTERN = r"^[A-Za-z0-9_.-]+$"
_dummy_hash_cache: str | None = None


def _dummy_hash() -> str:
    """用于拉平「账号不存在」分支的耗时，避免时序侧信道。"""
    global _dummy_hash_cache
    if _dummy_hash_cache is None:
        _dummy_hash_cache = security.hash_password("__no_such_user__")
    return _dummy_hash_cache


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=_USERNAME_PATTERN)
    password: str = Field(min_length=MIN_PASSWORD_LEN, max_length=128)
    nickname: str = Field(default="学习者", min_length=1, max_length=64)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    username: str
    request_id: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    request_id: str


class MeResponse(BaseModel):
    user_id: str
    username: str
    nickname: str
    request_id: str


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=security.create_access_token(user.id),
        refresh_token=security.create_refresh_token(user.id),
        expires_in=security.access_token_ttl_seconds(),
        user_id=user.id,
        username=user.username,
        request_id=trace.get_request_id() or "",
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(
    body: RegisterRequest,
    session: Session = Depends(get_session),
) -> TokenResponse:
    """注册并直接返回令牌（省一次登录往返）。用户名统一小写，避免 Admin/admin 混淆。"""
    username = body.username.strip().lower()
    repo = UserRepository(session)
    if repo.get_by_username(username) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名已被占用")

    user = repo.create_user(
        username=username,
        password_hash=security.hash_password(body.password),
        nickname=body.nickname.strip() or "学习者",
    )
    session.commit()
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    session: Session = Depends(get_session),
) -> TokenResponse:
    username = body.username.strip().lower()
    repo = UserRepository(session)
    user = repo.get_by_username(username)
    if user is None:
        security.verify_password(body.password, _dummy_hash())  # 拉平耗时
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not security.verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码不正确",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token_response(user)


@router.post("/refresh", response_model=AccessTokenResponse)
def refresh(body: RefreshRequest) -> AccessTokenResponse:
    """用 refresh token 换发新的 access token（refresh 自身不续期，到期需重新登录）。"""
    try:
        user_id = security.decode_token(body.refresh_token, expected_type="refresh")
    except security.TokenExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 已过期，请重新登录"
        ) from exc
    except security.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效"
        ) from exc

    return AccessTokenResponse(
        access_token=security.create_access_token(user_id),
        expires_in=security.access_token_ttl_seconds(),
        request_id=trace.get_request_id() or "",
    )


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=user.id,
        username=user.username,
        nickname=user.nickname,
        request_id=trace.get_request_id() or "",
    )
