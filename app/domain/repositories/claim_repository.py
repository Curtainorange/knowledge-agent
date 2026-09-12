"""主张仓储：L2 冲突检测的最小比对粒度存取，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.models.claim import Claim
from app.domain.repositories.base import BaseRepository


class ClaimRepository(BaseRepository[Claim]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        knowledge_item_id: str,
        statement: str,
        topic: str = "",
        polarity: int = 0,
        strength: float = 0.5,
        confidence: float = 0.5,
        embedding: bytes | None = None,
    ) -> Claim:
        claim = Claim(
            user_id=user_id,
            knowledge_item_id=knowledge_item_id,
            statement=statement,
            topic=topic,
            polarity=int(polarity),
            strength=float(strength),
            confidence=float(confidence),
            embedding=embedding,
        )
        self._session.add(claim)
        self._session.flush()
        return claim

    def list_by_user(self, user_id: str) -> list[Claim]:
        self._guard(user_id)
        stmt = select(Claim).where(Claim.user_id == user_id).order_by(Claim.created_at.asc())
        return list(self._session.scalars(stmt))

    def list_by_item(self, item_id: str) -> list[Claim]:
        stmt = (
            select(Claim)
            .where(Claim.knowledge_item_id == item_id)
            .order_by(Claim.created_at.asc())
        )
        return list(self._session.scalars(stmt))

    def get(self, claim_id: str) -> Claim | None:
        claim = self._session.get(Claim, claim_id)
        if claim is None:
            return None
        self._guard(claim.user_id)
        return claim

    def count_by_user(self, user_id: str) -> int:
        self._guard(user_id)
        stmt = select(func.count()).select_from(Claim).where(Claim.user_id == user_id)
        return int(self._session.scalar(stmt) or 0)

    def delete_by_item(self, item_id: str) -> int:
        """删除某条目的全部主张（force 重扫前重置）。"""
        rows = self.list_by_item(item_id)
        for row in rows:
            self._session.delete(row)
        self._session.flush()
        return len(rows)
