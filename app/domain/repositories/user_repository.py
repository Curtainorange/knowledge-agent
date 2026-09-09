"""用户仓储（P0 最小实现：创建/查询）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.user import User
from app.domain.repositories.base import BaseRepository


class UserRepository(BaseRepository[User]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(self, nickname: str = "学习者", push_frequency: str = "weekly") -> User:
        user = User(nickname=nickname, push_frequency=push_frequency)
        self._session.add(user)
        self._session.flush()
        return user

    def get(self, user_id: str) -> User | None:
        self._guard(user_id)
        return self._session.get(User, user_id)