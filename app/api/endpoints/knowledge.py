"""知识接入端点（D4）：手动录入知识条目，入口即算 embedding。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core import trace
from app.ingestion.service import IngestionService
from app.retrieval.embedding import build_embedding

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class KnowledgeCreateResponse(BaseModel):
    item_id: str
    embed_status: str
    request_id: str


@router.post("/items", response_model=KnowledgeCreateResponse)
def create_item(
    body: KnowledgeCreate,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeCreateResponse:
    svc = IngestionService(session, build_embedding())
    item = svc.add_knowledge(user_id=user_id, title=body.title, content=body.content, tags=body.tags)
    session.commit()  # 条目 + embedding 同事务持久化
    return KnowledgeCreateResponse(
        item_id=item.id, embed_status=item.embed_status, request_id=trace.get_request_id() or ""
    )