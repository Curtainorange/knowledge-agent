"""知识接入端点（D4）：录入 + 列表 / 详情 / 更新 / 软删，入口即算 embedding。

- 录入与更新都会（重）算 embedding；向量化失败只降级召回，不阻断落库（可靠-4）。
- 所有单条操作经仓储 user_id 作用域，越权抛 PermissionError → 映射为 403（不泄漏存在性）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.core import trace
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.ingestion.service import IngestionService
from app.retrieval.embedding import build_embedding

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge"])

def _svc(session: Session) -> IngestionService:
    return IngestionService(session, build_embedding())


class KnowledgeCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)


class KnowledgeCreateResponse(BaseModel):
    item_id: str
    embed_status: str
    request_id: str


class KnowledgeBrief(BaseModel):
    """列表项：只给摘要，不给全文与向量。"""

    item_id: str
    title: str
    snippet: str
    tags: list[str] = Field(default_factory=list)
    read_progress: float
    embed_status: str
    created_at: datetime


class KnowledgeListResponse(BaseModel):
    items: list[KnowledgeBrief]
    total: int
    limit: int
    offset: int
    request_id: str


class KnowledgeDetailResponse(BaseModel):
    item_id: str
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    read_progress: float
    embed_status: str
    source: str
    created_at: datetime
    updated_at: datetime
    request_id: str


class KnowledgeUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=256)
    content: str | None = Field(default=None, min_length=1)
    tags: list[str] | None = None
    read_progress: float | None = Field(default=None, ge=0.0, le=1.0, description="阅读进度 0..1")


class KnowledgeUpdateResponse(BaseModel):
    item_id: str
    embed_status: str
    updated_fields: list[str]
    request_id: str


class KnowledgeDeleteResponse(BaseModel):
    item_id: str
    deleted: bool
    request_id: str


@router.post("/items", response_model=KnowledgeCreateResponse)
def create_item(
    body: KnowledgeCreate,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeCreateResponse:
    svc = _svc(session)
    item = svc.add_knowledge(user_id=user_id, title=body.title, content=body.content, tags=body.tags)
    session.commit()  # service 内部已分两段提交（先落库、后补向量），此处兜底
    return KnowledgeCreateResponse(
        item_id=item.id, embed_status=item.embed_status, request_id=trace.get_request_id() or ""
    )


@router.get("/items", response_model=KnowledgeListResponse)
def list_items(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeListResponse:
    """分页列出当前用户未删除条目（默认 20 条，最多 100 条）。"""
    repo = KnowledgeRepository(session, user_id=user_id)
    total = repo.count_active(user_id)
    rows = repo.page_active(user_id, limit=limit, offset=offset)
    return KnowledgeListResponse(
        items=[
            KnowledgeBrief(
                item_id=r.id,
                title=r.title,
                snippet=r.snippet,
                tags=list(r.tags or []),
                read_progress=r.read_progress,
                embed_status=r.embed_status,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        request_id=trace.get_request_id() or "",
    )


@router.get("/items/{item_id}", response_model=KnowledgeDetailResponse)
def get_item(
    item_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeDetailResponse:
    """读取单条全文（不含向量）。"""
    try:
        item = KnowledgeRepository(session, user_id=user_id).get(item_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该条目") from exc
    if item is None or item.is_deleted:
        raise HTTPException(status_code=404, detail="条目不存在")
    return KnowledgeDetailResponse(
        item_id=item.id,
        title=item.title,
        content=item.raw_content,
        tags=list(item.tags or []),
        read_progress=item.read_progress,
        embed_status=item.embed_status,
        source=item.source,
        created_at=item.created_at,
        updated_at=item.updated_at,
        request_id=trace.get_request_id() or "",
    )


@router.patch("/items/{item_id}", response_model=KnowledgeUpdateResponse)
def update_item(
    item_id: str,
    body: KnowledgeUpdateRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeUpdateResponse:
    """局部更新；改了标题或正文才重算 embedding，避免无谓向量开销。"""
    fields = {
        k: v
        for k, v in {
            "title": body.title,
            "content": body.content,
            "tags": body.tags,
            "read_progress": body.read_progress,
        }.items()
        if v is not None
    }
    if not fields:
        raise HTTPException(status_code=400, detail="至少提供一个待更新字段")

    try:
        item = _svc(session).update_knowledge(user_id=user_id, item_id=item_id, **fields)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该条目") from exc
    if item is None or item.is_deleted:
        raise HTTPException(status_code=404, detail="条目不存在")
    session.commit()
    return KnowledgeUpdateResponse(
        item_id=item.id,
        embed_status=item.embed_status,
        updated_fields=sorted(fields),
        request_id=trace.get_request_id() or "",
    )


@router.delete("/items/{item_id}", response_model=KnowledgeDeleteResponse)
def delete_item(
    item_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeDeleteResponse:
    """软删：置 is_deleted，保留原文以备恢复与审计（需求安全-5）。"""
    try:
        item = _svc(session).delete_knowledge(user_id=user_id, item_id=item_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该条目") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    session.commit()
    return KnowledgeDeleteResponse(
        item_id=item.id, deleted=True, request_id=trace.get_request_id() or ""
    )


@router.get("/export")
def export_items(
    format: str = Query(default="markdown", pattern="^(markdown|json)$"),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> Response:
    """导出当前用户的全部知识条目（markdown / json），供备份与迁移。

    与列表接口不同，这里刻意取**全量**：导出要的是「一条不落」，分页反而添乱。
    响应带 Content-Disposition，浏览器会直接下载文件。
    """
    items = KnowledgeRepository(session, user_id=user_id).list_active(user_id)
    exported_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if format == "json":
        payload = {
            "exported_at": exported_at,
            "total": len(items),
            "items": [
                {
                    "item_id": i.id,
                    "title": i.title,
                    "content": i.raw_content,
                    "tags": list(i.tags or []),
                    "source": i.source,
                    "read_progress": i.read_progress,
                    "created_at": i.created_at.isoformat() if i.created_at else None,
                    "updated_at": i.updated_at.isoformat() if i.updated_at else None,
                }
                for i in items
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2)
        media_type, filename = "application/json", "knowledge-export.json"
    else:
        lines: list[str] = [
            "# 知识库导出",
            "",
            f"> 导出时间：{exported_at} · 共 {len(items)} 条",
            "",
        ]
        for i in items:
            lines.append(f"## {i.title}")
            lines.append("")
            lines.append(i.raw_content or "")
            lines.append("")
            meta: list[str] = []
            if i.tags:
                meta.append("标签：" + "、".join(str(t) for t in i.tags))
            if i.source:
                meta.append(f"来源：{i.source}")
            if i.created_at:
                meta.append("创建：" + i.created_at.strftime("%Y-%m-%d %H:%M"))
            lines.append("> " + " · ".join(meta))
            lines.append("")
        body = "\n".join(lines)
        media_type, filename = "text/markdown; charset=utf-8", "knowledge-export.md"

    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
