"""认知诊断报告仓储（L5 归因诊断的持久化层）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.cognitive_diagnosis import DIAGNOSIS_STATES, CognitiveDiagnosis
from app.domain.repositories.base import BaseRepository


class CognitiveDiagnosisRepository(BaseRepository[CognitiveDiagnosis]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        pattern: str = "",
        root_cause: str = "",
        confidence: float = 0.5,
        suggested_action: str = "",
        reasoning_chain: list | None = None,
    ) -> CognitiveDiagnosis:
        diagnosis = CognitiveDiagnosis(
            user_id=user_id,
            pattern=pattern,
            root_cause=root_cause,
            confidence=float(confidence),
            suggested_action=suggested_action,
            reasoning_chain=reasoning_chain or [],
            status="pending",
        )
        self._session.add(diagnosis)
        self._session.flush()
        return diagnosis

    def get(self, diagnosis_id: str) -> CognitiveDiagnosis | None:
        diagnosis = self._session.get(CognitiveDiagnosis, diagnosis_id)
        if diagnosis is None:
            return None
        self._guard(diagnosis.user_id)
        return diagnosis

    def latest_for_user(self, user_id: str) -> CognitiveDiagnosis | None:
        self._guard(user_id)
        stmt = (
            select(CognitiveDiagnosis)
            .where(CognitiveDiagnosis.user_id == user_id)
            .order_by(CognitiveDiagnosis.created_at.desc(), CognitiveDiagnosis.id.desc())
        )
        return self._session.scalars(stmt).first()

    def set_status(self, diagnosis: CognitiveDiagnosis, status: str) -> None:
        if status not in DIAGNOSIS_STATES:
            raise ValueError(f"非法诊断状态：{status!r}")
        diagnosis.status = status
        self._session.flush()
