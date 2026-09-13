"""推送端点（ADR-14）：用户查看待处理的推送，标记已读。

P0 推送通道是「app 内通知」——`PushJob.status == pending` 即待用户查看，
用户查看后调 delivered 标记已读。真实邮件/推送通道留待外部集成（B 组）。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core import trace
from app.domain.repositories.push_repository import PushJobRepository

router = APIRouter(prefix="/api/v1/push", tags=["push"])


class PushJobOut(BaseModel):
    job_id: str
    push_type: str
    title: str
    body: str
    created_at: str = ""


class PushJobsResponse(BaseModel):
    items: list[PushJobOut]
    total: int
    request_id: str


class DeliveredResponse(BaseModel):
    job_id: str
    request_id: str


@router.get("/jobs", response_model=PushJobsResponse)
def list_pending(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> PushJobsResponse:
    jobs = PushJobRepository(session, user_id=user_id).list_pending(user_id)
    return PushJobsResponse(
        items=[
            PushJobOut(
                job_id=j.id,
                push_type=j.push_type,
                title=j.title,
                body=j.body,
                created_at=j.created_at.isoformat() if j.created_at else "",
            )
            for j in jobs
        ],
        total=len(jobs),
        request_id=trace.get_request_id() or "",
    )


@router.post("/jobs/{job_id}/delivered", response_model=DeliveredResponse)
def mark_delivered(
    job_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> DeliveredResponse:
    repo = PushJobRepository(session, user_id=user_id)
    job = repo.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="推送不存在")
    repo.set_status(job, "delivered")
    session.commit()
    return DeliveredResponse(job_id=job_id, request_id=trace.get_request_id() or "")
