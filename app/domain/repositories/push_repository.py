"""推送任务与日志仓储（ADR-14 推送与抑制服务的持久化层）。

PushJob 与 PushLog 总是成对读写（生成任务 → 发送 → 记日志），放一个文件避免两处
维护同一套作用域规则。作用域铁律同其他仓储：写必须带 user_id，读 `_guard` 校验归属。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.push_job import PUSH_JOB_STATES, PushJob
from app.domain.models.push_log import FEEDBACK_STATES, PushLog
from app.domain.repositories.base import BaseRepository


class PushJobRepository(BaseRepository[PushJob]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        push_type: str,
        content_hash: str,
        title: str = "",
        body: str = "",
        payload: dict | None = None,
        channel: str = "app",
    ) -> PushJob:
        job = PushJob(
            user_id=user_id,
            push_type=push_type,
            content_hash=content_hash,
            title=title,
            body=body,
            payload=payload or {},
            channel=channel,
            status="pending",
        )
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: str) -> PushJob | None:
        job = self._session.get(PushJob, job_id)
        if job is None:
            return None
        self._guard(job.user_id)
        return job

    def list_pending(self, user_id: str, *, limit: int = 20) -> list[PushJob]:
        self._guard(user_id)
        stmt = (
            select(PushJob)
            .where(PushJob.user_id == user_id, PushJob.status == "pending")
            .order_by(PushJob.created_at.asc(), PushJob.id.asc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def set_status(self, job: PushJob, status: str, error: str = "") -> None:
        if status not in PUSH_JOB_STATES:
            raise ValueError(f"非法推送任务状态：{status!r}")
        job.status = status
        if error:
            job.last_error = error[:1000]
        if status in ("sent", "delivered"):
            job.sent_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        self._session.flush()


class PushLogRepository(BaseRepository[PushLog]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def exists(self, user_id: str, content_hash: str) -> bool:
        """去重：同内容 hash 是否已推送过（模型层 unique 是第一道，这里是显式查询）。"""
        self._guard(user_id)
        stmt = select(PushLog.id).where(
            PushLog.user_id == user_id, PushLog.content_hash == content_hash
        )
        return self._session.scalars(stmt).first() is not None

    def create(
        self,
        *,
        user_id: str,
        push_type: str,
        content_hash: str,
        channel: str = "app",
    ) -> PushLog:
        log = PushLog(
            user_id=user_id,
            push_type=push_type,
            content_hash=content_hash,
            channel=channel,
            sent_at=datetime.now(timezone.utc).replace(tzinfo=None),
            user_feedback="none",
        )
        self._session.add(log)
        self._session.flush()
        return log

    def set_feedback(self, log: PushLog, feedback: str) -> None:
        if feedback not in FEEDBACK_STATES:
            raise ValueError(f"非法推送反馈：{feedback!r}")
        log.user_feedback = feedback
        self._session.flush()
