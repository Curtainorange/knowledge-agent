"""L2 冲突检测端点（系统设计 §5.2）：扫描 / 冲突列表 / 用户反馈。

- `POST /l2/scan`：手动触发一次增量扫描（主张提取 → 候选对 → LLM 判定 → 入库）。
  周扫/实时触发后续接调度与事件链路，P0 先给手动入口。
- `GET /l2/conflicts`：按 user_state 过滤冲突（unseen / ignored / accepted）。
- `PATCH /l2/conflicts/{id}/state`：用户反馈，驱动误报抑制（忽略同类型 ≥N 次收敛）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agent.l2_orchestrator import L2Orchestrator
from app.api.deps import get_gateway, get_session, get_user_id
from app.core import trace
from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import VALID_STATES, ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.gateway import ModelGateway

router = APIRouter(prefix="/api/v1/l2", tags=["l2"])


class L2ScanResponse(BaseModel):
    scanned_items: int
    claims_extracted: int
    extraction_failures: int
    pairs_judged: int
    conflicts_found: int
    conflicts_suppressed: int
    conflict_ids: list[str]
    request_id: str


class ConflictOut(BaseModel):
    conflict_id: str
    item_a_id: str
    item_b_id: str
    title_a: str
    title_b: str
    claim_a: str
    claim_b: str
    conflict_type: str
    detail: str
    suggestion: str
    confidence: float
    user_state: str
    created_at: datetime


class ConflictListResponse(BaseModel):
    items: list[ConflictOut]
    total: int
    request_id: str


class ConflictStateRequest(BaseModel):
    state: Literal["unseen", "ignored", "accepted"]


class ConflictStateResponse(BaseModel):
    conflict_id: str
    user_state: str
    request_id: str


@router.post("/scan", response_model=L2ScanResponse)
def scan(
    user_id: str = Depends(get_user_id),
    gateway: ModelGateway = Depends(get_gateway),
    session: Session = Depends(get_session),
) -> L2ScanResponse:
    """触发一次增量扫描：主张提取 → 候选对 → LLM 判定 → 冲突入库。"""
    result = L2Orchestrator(gateway, session).scan(user_id=user_id)
    return L2ScanResponse(
        scanned_items=result.scanned_items,
        claims_extracted=result.claims_extracted,
        extraction_failures=result.extraction_failures,
        pairs_judged=result.pairs_judged,
        conflicts_found=result.conflicts_found,
        conflicts_suppressed=result.conflicts_suppressed,
        conflict_ids=result.conflict_ids,
        request_id=trace.get_request_id() or "",
    )


@router.get("/conflicts", response_model=ConflictListResponse)
def list_conflicts(
    state: str | None = Query(default=None, description=f"按状态过滤：{'/'.join(VALID_STATES)}"),
    limit: int = Query(default=50, ge=1, le=200),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConflictListResponse:
    """列出当前用户的冲突（默认全部状态，最新在前）。"""
    if state is not None and state not in VALID_STATES:
        raise HTTPException(status_code=422, detail=f"非法状态：{state}")

    xrepo = ConflictRepository(session, user_id=user_id)
    krepo = KnowledgeRepository(session, user_id=user_id)
    crepo = ClaimRepository(session, user_id=user_id)
    rows = xrepo.list_by_user(user_id, state=state, limit=limit)

    items = []
    for row in rows:
        item_a = krepo.get(row.item_a_id)
        item_b = krepo.get(row.item_b_id)
        claim_a = crepo.get(row.claim_a_id) if row.claim_a_id else None
        claim_b = crepo.get(row.claim_b_id) if row.claim_b_id else None
        items.append(
            ConflictOut(
                conflict_id=row.id,
                item_a_id=row.item_a_id,
                item_b_id=row.item_b_id,
                title_a=item_a.title if item_a else "（条目已删除）",
                title_b=item_b.title if item_b else "（条目已删除）",
                claim_a=claim_a.statement if claim_a else "",
                claim_b=claim_b.statement if claim_b else "",
                conflict_type=row.conflict_type,
                detail=row.detail,
                suggestion=row.suggestion,
                confidence=row.confidence,
                user_state=row.user_state,
                created_at=row.created_at,
            )
        )
    return ConflictListResponse(
        items=items, total=len(items), request_id=trace.get_request_id() or ""
    )


@router.patch("/conflicts/{conflict_id}/state", response_model=ConflictStateResponse)
def set_conflict_state(
    conflict_id: str,
    body: ConflictStateRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ConflictStateResponse:
    """更新冲突反馈状态（unseen / ignored / accepted）。"""
    xrepo = ConflictRepository(session, user_id=user_id)
    try:
        conflict = xrepo.get(conflict_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该冲突") from exc
    if conflict is None:
        raise HTTPException(status_code=404, detail="冲突不存在")

    previous_state = conflict.user_state
    xrepo.set_state(conflict, body.state)
    session.commit()

    from app.feedback import events

    # 反馈是误报抑制的输入（同类型被忽略 ≥N 次即收敛），也是 L5 判断"用户是否采纳建议"的信号
    events.record(
        session, user_id=user_id, event_type=events.L2_CONFLICT_FEEDBACK,
        payload={
            "conflict_id": conflict.id,
            "from_state": previous_state,
            "to_state": body.state,
            "conflict_type": conflict.conflict_type,
            "confidence": conflict.confidence,
        },
    )
    return ConflictStateResponse(
        conflict_id=conflict.id,
        user_state=conflict.user_state,
        request_id=trace.get_request_id() or "",
    )
