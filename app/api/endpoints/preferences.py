"""用户偏好端点（系统设计 §7.1 / §8.1）：推送频率偏好读写。

推送频率三档（UC-G-02 主动推送偏好管理）：
- weekly（默认）：每周一推认知简报
- daily：每日轻推单条冲突/追问
- quiet：免打扰，仅重大诊断才推

偏好是 ADR-14 推送与抑制服务的输入之一——quiet 用户在抑制决策里会被
「免打扰」规则拦下，不产生常规推送。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_session
from app.core import trace
from app.domain.models.user import User
from app.domain.repositories.user_repository import UserRepository

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])

VALID_FREQUENCIES = ("weekly", "daily", "quiet")


class PreferencesOut(BaseModel):
    push_frequency: str
    request_id: str


class PreferencesUpdate(BaseModel):
    push_frequency: str = Field(pattern="^(weekly|daily|quiet)$")


@router.get("", response_model=PreferencesOut)
def get_preferences(
    user: User = Depends(get_current_user),
) -> PreferencesOut:
    return PreferencesOut(
        push_frequency=user.push_frequency or "weekly",
        request_id=trace.get_request_id() or "",
    )


@router.put("", response_model=PreferencesOut)
def update_preferences(
    body: PreferencesUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> PreferencesOut:
    UserRepository(session).set_push_frequency(user, body.push_frequency)
    session.commit()
    return PreferencesOut(
        push_frequency=body.push_frequency,
        request_id=trace.get_request_id() or "",
    )
