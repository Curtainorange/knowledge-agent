"""认证端点：注册 / 登录 / 刷新令牌 / 当前用户（见系统设计 9.3.1）。

设计要点：
- 登录失败**不区分**「用户不存在」与「密码错误」，且用户不存在时仍走一次哈希校验，
  避免通过响应内容或耗时差异枚举账号。
- refresh token 只能用于换发 access token，不能直接访问业务接口（decode 时校验 typ）。
- 密码明文只在请求体内短暂存在，不落库、不入日志。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_session
from app.core import security, trace
from app.core.config import settings
from app.core.ratelimit import FailureThrottle
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


# ---- 登录失败限流 ---------------------------------------------------------
# 账号与来源 IP 各一个计数器：前者防针对单一账号的爆破，后者防单 IP 广撒网。
# 惰性构造，便于测试调整阈值后重新生效（见 reset_login_throttle）。

_account_throttle: FailureThrottle | None = None
_source_throttle: FailureThrottle | None = None


def _throttles() -> tuple[FailureThrottle, FailureThrottle]:
    global _account_throttle, _source_throttle
    if _account_throttle is None:
        _account_throttle = FailureThrottle(
            max_attempts=settings.login_max_attempts,
            window_seconds=settings.login_window_seconds,
            lock_seconds=settings.login_lock_seconds,
        )
    if _source_throttle is None:
        _source_throttle = FailureThrottle(
            max_attempts=settings.login_ip_max_attempts,
            window_seconds=settings.login_window_seconds,
            lock_seconds=settings.login_lock_seconds,
        )
    return _account_throttle, _source_throttle


def reset_login_throttle() -> None:
    """重建限流器并清空计数（仅供测试在改动阈值后调用）。"""
    global _account_throttle, _source_throttle
    _account_throttle = None
    _source_throttle = None


def _login_keys(request: Request, username: str) -> tuple[str, str]:
    ip = request.client.host if request.client else "unknown"
    return f"user:{username}", f"ip:{ip}"


def _ensure_not_throttled(request: Request, username: str) -> None:
    if not settings.login_throttle_enabled:
        return
    account, source = _throttles()
    user_key, ip_key = _login_keys(request, username)
    wait = max(account.retry_after(user_key), source.retry_after(ip_key))
    if wait > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"登录失败次数过多，请 {wait} 秒后再试",
            headers={"Retry-After": str(wait)},
        )


def _record_login_failure(request: Request, username: str) -> None:
    if not settings.login_throttle_enabled:
        return
    account, source = _throttles()
    user_key, ip_key = _login_keys(request, username)
    account.record_failure(user_key)
    source.record_failure(ip_key)


def _reset_login_failures(request: Request, username: str) -> None:
    if not settings.login_throttle_enabled:
        return
    account, source = _throttles()
    user_key, ip_key = _login_keys(request, username)
    account.reset(user_key)
    source.reset(ip_key)


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


def _invalid_credentials() -> HTTPException:
    """登录失败的统一响应：不区分「账号不存在」与「密码错误」，避免被用来枚举账号。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="用户名或密码不正确",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> TokenResponse:
    username = body.username.strip().lower()
    _ensure_not_throttled(request, username)

    repo = UserRepository(session)
    user = repo.get_by_username(username)
    if user is None:
        security.verify_password(body.password, _dummy_hash())  # 拉平耗时，缩小账号枚举的时序差
        _record_login_failure(request, username)
        raise _invalid_credentials()
    if not security.verify_password(body.password, user.password_hash):
        _record_login_failure(request, username)
        raise _invalid_credentials()

    _reset_login_failures(request, username)
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
