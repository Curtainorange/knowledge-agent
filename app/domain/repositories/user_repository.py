"""用户仓储：账号创建与按用户名/ID 查询。

密码哈希由 `app.core.security` 生成，仓储只负责存取哈希串，不接触明文。
"""
from __future__ import annotations

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

    def set_password_hash(self, user: User, password_hash: str) -> None:
        user.password_hash = password_hash
        self._session.flush()
