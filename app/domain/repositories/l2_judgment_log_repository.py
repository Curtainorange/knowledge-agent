"""L2 判定日志仓储：写入与最近查询，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.l2_judgment_log import L2JudgmentLog
from app.domain.repositories.base import BaseRepository


class L2JudgmentLogRepository(BaseRepository[L2JudgmentLog]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        pair_key: str,
        claim_a_id: str,
        claim_b_id: str,
        source_a: str,
        source_b: str,
        title_a: str,
        title_b: str,
        claim_a_text: str,
        claim_b_text: str,
        relation: str,
        conflict_type: str = "",
        confidence: float = 0.0,
        polarity_a: int = 0,
        polarity_b: int = 0,
        detail: str = "",
    ) -> L2JudgmentLog:
        row = L2JudgmentLog(
            user_id=user_id,
            pair_key=pair_key,
            claim_a_id=claim_a_id,
            claim_b_id=claim_b_id,
            source_a=source_a,
            source_b=source_b,
            title_a=title_a[:256],
            title_b=title_b[:256],
            claim_a_text=claim_a_text,
            claim_b_text=claim_b_text,
            relation=relation,
            conflict_type=conflict_type[:32],
            confidence=float(confidence),
            polarity_a=int(polarity_a),
            polarity_b=int(polarity_b),
            detail=detail,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def list_recent(self, user_id: str, *, limit: int = 50) -> list[L2JudgmentLog]:
        self._guard(user_id)
        stmt = (
            select(L2JudgmentLog)
            .where(L2JudgmentLog.user_id == user_id)
            .order_by(L2JudgmentLog.created_at.desc(), L2JudgmentLog.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def count_by_relation(self, user_id: str, relation: str) -> int:
        self._guard(user_id)
        stmt = select(L2JudgmentLog).where(
            L2JudgmentLog.user_id == user_id,
            L2JudgmentLog.relation == relation,
        )
        return len(list(self._session.scalars(stmt)))
