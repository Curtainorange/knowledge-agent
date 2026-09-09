"""成本日志仓储：写入模型调用成本（维护-1，成本埋点）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.domain.models.cost_log import CostLog
from app.domain.repositories.base import BaseRepository


class CostLogRepository(BaseRepository[CostLog]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        task_type: str,
        model: str,
        reasoning: bool,
        request_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_cost: float,
    ) -> CostLog:
        log = CostLog(
            user_id=user_id,
            task_type=task_type,
            model=model,
            reasoning=reasoning,
            request_id=request_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost=round(estimated_cost, 6),
            created_at=datetime.now(timezone.utc),
        )
        self._session.add(log)
        return log  # 提交由外层事务统一负责