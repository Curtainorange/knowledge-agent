"""冲突仓储：L2 冲突检测产物的读写与用户反馈状态，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models.conflict import Conflict
from app.domain.repositories.base import BaseRepository

VALID_STATES = ("unseen", "ignored", "accepted")


def make_pair_key(claim_a_id: str, claim_b_id: str) -> str:
    """无序对键：同一对主张无论判定顺序如何都映射到同一个键。"""
    left, right = sorted([claim_a_id or "", claim_b_id or ""])
    return f"{left}|{right}"


class ConflictRepository(BaseRepository[Conflict]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        item_a_id: str,
        item_b_id: str,
        claim_a_id: str | None,
        claim_b_id: str | None,
        conflict_type: str,
        detail: str = "",
        suggestion: str = "",
        confidence: float = 0.0,
    ) -> Conflict:
        conflict = Conflict(
            user_id=user_id,
            item_a_id=item_a_id,
            item_b_id=item_b_id,
            claim_a_id=claim_a_id,
            claim_b_id=claim_b_id,
            pair_key=make_pair_key(claim_a_id or "", claim_b_id or ""),
            conflict_type=conflict_type,
            detail=detail,
            suggestion=suggestion,
            confidence=float(confidence),
            user_state="unseen",
        )
        self._session.add(conflict)
        self._session.flush()
        return conflict

    def get(self, conflict_id: str) -> Conflict | None:
        conflict = self._session.get(Conflict, conflict_id)
        if conflict is None:
            return None
        self._guard(conflict.user_id)
        return conflict

    def find_by_pair(self, user_id: str, pair_key: str) -> Conflict | None:
        """同一对主张是否已判定过（无论结果），避免重复入库。"""
        self._guard(user_id)
        stmt = select(Conflict).where(Conflict.user_id == user_id, Conflict.pair_key == pair_key)
        return self._session.scalars(stmt).first()

    def list_by_user(
        self, user_id: str, *, state: str | None = None, limit: int = 50
    ) -> list[Conflict]:
        self._guard(user_id)
        stmt = select(Conflict).where(Conflict.user_id == user_id)
        if state:
            stmt = stmt.where(Conflict.user_state == state)
        stmt = stmt.order_by(Conflict.created_at.desc(), Conflict.id.asc()).limit(limit)
        return list(self._session.scalars(stmt))

    def count_by_state(self, user_id: str) -> dict[str, int]:
        self._guard(user_id)
        stmt = (
            select(Conflict.user_state, func.count())
            .where(Conflict.user_id == user_id)
            .group_by(Conflict.user_state)
        )
        return {str(state): int(count) for state, count in self._session.execute(stmt)}

    def ignored_type_counts(self, user_id: str) -> dict[str, int]:
        """各类冲突被忽略的次数——用于收敛同类推荐（UC-L2-03 扩展 2a）。"""
        self._guard(user_id)
        stmt = (
            select(Conflict.conflict_type, func.count())
            .where(Conflict.user_id == user_id, Conflict.user_state == "ignored")
            .group_by(Conflict.conflict_type)
        )
        return {str(t): int(c) for t, c in self._session.execute(stmt)}

    def set_state(self, conflict: Conflict, state: str) -> Conflict:
        if state not in VALID_STATES:
            raise ValueError(f"非法冲突状态：{state!r}")
        conflict.user_state = state
        self._session.flush()
        return conflict
