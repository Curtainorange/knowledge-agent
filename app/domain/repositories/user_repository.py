"""用户仓储：账号创建与按用户名/ID 查询。

密码哈希由 `app.core.security` 生成，仓储只负责存取哈希串，不接触明文。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.user import User
from app.domain.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create_user(
        self, *, username: str, password_hash: str, nickname: str = "学习者"
    ) -> User:
        user = User(username=username, password_hash=password_hash, nickname=nickname)
        self._session.add(user)
        self._session.flush()
        return user

    def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return self._session.scalars(stmt).first()

    def get(self, user_id: str) -> User | None:
        self._guard(user_id)
        return self._session.get(User, user_id)

    def list_ids(self) -> list[str]:
        """全部用户 id（周期性任务需要遍历用户，如 L2 周扫）。"""
        return list(self._session.scalars(select(User.id)))

    def set_password_hash(self, user: User, password_hash: str) -> None:
        user.password_hash = password_hash
        self._session.flush()

    def bump_token_version(self, user: User) -> None:
        """令牌版本 +1：所有已签发令牌立即失效（改密 / 注销用）。"""
        user.token_version = int(user.token_version or 0) + 1
        self._session.flush()

    def mark_deleted(self, user: User, when: datetime | None = None) -> None:
        """软删账号：置注销时间戳，数据保留（审计与保留期）。"""
        user.deleted_at = when or datetime.now(timezone.utc).replace(tzinfo=None)
        self._session.flush()

    def set_push_frequency(self, user: User, frequency: str) -> None:
        """更新推送频率偏好（weekly / daily / quiet，见系统设计 §7.1）。"""
        user.push_frequency = frequency
        self._session.flush()
