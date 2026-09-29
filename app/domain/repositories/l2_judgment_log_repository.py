"""L2 判定日志仓储：写入与查询，强制 user_id 作用域。

日志 append-only：复核结论写新行（review_of_id 指向原判），不覆盖原判——
原判是排查事实。终判 = 该对 review_of_id 非空的最新行，无复核行则取原判行
（latest_for_pair 实现此收口）。
"""
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
        calibrated_confidence: float = 0.0,
        sim: float | None = None,
        review_of_id: str = "",
        review_state: str = "none",
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
            calibrated_confidence=float(calibrated_confidence),
            sim=sim,
            review_of_id=review_of_id,
            review_state=review_state[:16],
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

    def exists_pair(self, user_id: str, pair_key: str) -> bool:
        self._guard(user_id)
        stmt = select(L2JudgmentLog.id).where(
            L2JudgmentLog.user_id == user_id,
            L2JudgmentLog.pair_key == pair_key,
        ).limit(1)
        return self._session.scalars(stmt).first() is not None

    def latest_for_pair(self, user_id: str, pair_key: str) -> L2JudgmentLog | None:
        """终判收口：优先取该对最新的复核行，无则取最新原判行。"""
        self._guard(user_id)
        rows = list(self._session.scalars(
            select(L2JudgmentLog)
            .where(
                L2JudgmentLog.user_id == user_id,
                L2JudgmentLog.pair_key == pair_key,
            )
            .order_by(L2JudgmentLog.created_at.asc(), L2JudgmentLog.id.asc())
        ))
        if not rows:
            return None
        reviews = [r for r in rows if r.review_of_id]
        return reviews[-1] if reviews else rows[-1]

    def initial_for_pair(self, user_id: str, pair_key: str) -> L2JudgmentLog | None:
        """原判行（review_of_id 为空的最新一条）——复核状态挂在它身上。"""
        self._guard(user_id)
        rows = list(self._session.scalars(
            select(L2JudgmentLog)
            .where(
                L2JudgmentLog.user_id == user_id,
                L2JudgmentLog.pair_key == pair_key,
                L2JudgmentLog.review_of_id == "",
            )
            .order_by(L2JudgmentLog.created_at.asc(), L2JudgmentLog.id.asc())
        ))
        return rows[-1] if rows else None

    def list_pending_review(self, user_id: str, *, limit: int = 20) -> list[L2JudgmentLog]:
        """待复核队列（review_state=pending 的原判行）——预算耗尽/复核失败的欠账。"""
        self._guard(user_id)
        stmt = (
            select(L2JudgmentLog)
            .where(
                L2JudgmentLog.user_id == user_id,
                L2JudgmentLog.review_state == "pending",
                L2JudgmentLog.review_of_id == "",
            )
            .order_by(L2JudgmentLog.created_at.asc(), L2JudgmentLog.id.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def mark_review_state(self, row: L2JudgmentLog, state: str) -> L2JudgmentLog:
        row.review_state = state[:16]
        self._session.flush()
        return row
