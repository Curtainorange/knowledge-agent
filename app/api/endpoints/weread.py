"""微信读书同步端点：查询同步状态 / 触发同步。

同步本身是**阻塞**操作（HTTP 逐本拉取 + 逐条向量化），所以这里用同步 `def` 而不是
`async def`——FastAPI 会把它放进线程池，不占住事件循环（避免同步期间整站卡住）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core import trace
from app.weread.client import WeReadError, WeReadNotConfigured
from app.weread.service import WeReadSyncService

router = APIRouter(prefix="/api/v1/weread", tags=["weread"])


class StatusResponse(BaseModel):
    configured: bool      # 是否已配置 API Key
    skill_version: str
    synced_items: int     # 已沉淀的微信读书条目数
    last_synced_at: datetime | None = None
    request_id: str


class SyncResponse(BaseModel):
    total_books: int      # 微信读书里「有笔记」的书总数
    scanned_books: int    # 本次扫描的书数
    created: int          # 新入库条目数
    skipped: int          # 已存在而跳过（含用户已删除的）
    failed_books: int     # 单本拉取失败数
    pending_books: int    # 因单次上限未扫描，可再点一次继续
    request_id: str


@router.get("/status", response_model=StatusResponse)
def get_status(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> StatusResponse:
    data = WeReadSyncService(session).status(user_id=user_id)
    return StatusResponse(**data, request_id=trace.get_request_id() or "")


@router.post("/sync", response_model=SyncResponse)
def sync_notes(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> SyncResponse:
    """把微信读书的划线 / 想法同步进知识库。幂等，可反复点击。"""
    try:
        result = WeReadSyncService(session).sync(user_id=user_id)
    except WeReadNotConfigured as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WeReadError as exc:
        # 上游（微信读书）问题 → 502，与「请求本身不合法」区分开
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SyncResponse(**result.as_dict(), request_id=trace.get_request_id() or "")
