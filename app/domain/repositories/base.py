"""仓储层（REPOSITORY）。

铁律：跨能力写操作一律经门店仓库存取，且仓储强制 user_id 过滤 —— 从结构上杜绝越权
（需求安全-3，架构图 9.3.1 越权防护），不依赖开发自觉。
"""
from __future__ import annotations

from typing import Generic, TypeVar

from sqlalchemy.orm import Session

from app.domain.models.base import Base

_T = TypeVar("_T", bound=Base)


class BaseRepository(Generic[_T]):
    """基于 SQLAlchemy Session 的基础仓储，绑定当前用户作用域。"""

    def __init__(self, session: Session, user_id: str | None = None):
        self._session = session
        self._user_id = user_id

    def _guard(self, owner_user_id: str) -> None:
        """结构性越权防护：绑定用户后，仅允许访问归属本用户的行。"""
        if self._user_id and owner_user_id != self._user_id:
            raise PermissionError(f"跨用户访问被拒（structural guard, user={owner_user_id}）")