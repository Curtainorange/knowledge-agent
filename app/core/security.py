"""认证基建：密码哈希（PBKDF2-HMAC-SHA256）与 JWT 签发 / 校验。

- 密码哈希用标准库实现，不引入 passlib / bcrypt：少一个依赖也少一个版本坑。
  存储格式 `pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>`，把迭代数写进串里，
  将来调高迭代数时旧密码仍可校验（校验用串内参数，重新哈希时才用新参数）。
- JWT 用 PyJWT；**解码时显式传算法白名单**，杜绝 alg 混淆（alg=none 类攻击）。
- 密钥优先取 `settings.jwt_secret`；未配置则进程内随机生成并告警，
  避免退化成"代码里硬编码一个公开弱密钥"这种更糟的情况。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app.core.config import settings

logger = logging.getLogger(__name__)

_ALGO_PREFIX = "pbkdf2_sha256"
_SALT_BYTES = 16


class TokenError(Exception):
    """令牌无效或不可用（基类）。"""


class TokenExpiredError(TokenError):
    """令牌已过期。"""


class InvalidTokenError(TokenError):
    """令牌签名不合法、类型不符或结构损坏。"""


_secret_cache: str | None = None


def _secret() -> str:
    """取签名密钥；未配置时生成进程内随机值（只生成一次）。"""
    global _secret_cache
    if _secret_cache is None:
        if settings.jwt_secret:
            _secret_cache = settings.jwt_secret
        else:
            _secret_cache = secrets.token_urlsafe(48)
            logger.warning(
                "JWT_SECRET 未配置，已生成进程内临时密钥：服务重启后所有已签发 token 将失效。"
                "生产环境请在 .env 中配置 JWT_SECRET。"
            )
    return _secret_cache


def reset_secret_cache() -> None:
    """清空密钥缓存（仅供测试在切换配置后调用）。"""
    global _secret_cache
    _secret_cache = None


# ---- 密码 -----------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def hash_password(password: str) -> str:
    """生成带盐密码哈希（每次调用盐不同，同一密码两次结果不同）。"""
    iterations = settings.password_pbkdf2_iterations
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO_PREFIX}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；用串内记录的参数重算，因此调高全局迭代数不会锁死老用户。"""
    try:
        algo, iterations_text, salt_text, digest_text = stored.split("$")
        if algo != _ALGO_PREFIX:
            return False
        iterations = int(iterations_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


# ---- JWT ------------------------------------------------------------------


def _encode(user_id: str, token_type: str, lifetime: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)


def create_access_token(user_id: str) -> str:
    return _encode(user_id, "access", timedelta(minutes=settings.access_token_minutes))


def create_refresh_token(user_id: str) -> str:
    return _encode(user_id, "refresh", timedelta(days=settings.refresh_token_days))


def decode_token(token: str, expected_type: str = "access") -> str:
    """校验令牌并返回 user_id。

    expected_type 用于区分 access / refresh：refresh 不能当 access 用，
    否则「拿长期凭证直接访问业务接口」会绕过短期过期策略。
    """
    try:
        payload = jwt.decode(token, _secret(), algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("令牌已过期") from exc
    except jwt.PyJWTError as exc:
        raise InvalidTokenError("令牌无效") from exc

    if payload.get("typ") != expected_type:
        raise InvalidTokenError(f"令牌类型不符，期望 {expected_type}")
    user_id = payload.get("sub")
    if not user_id:
        raise InvalidTokenError("令牌缺少 subject")
    return str(user_id)


def access_token_ttl_seconds() -> int:
    return settings.access_token_minutes * 60
