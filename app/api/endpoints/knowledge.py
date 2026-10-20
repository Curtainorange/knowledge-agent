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
    source: str
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


class TagCount(BaseModel):
    tag: str
    count: int


class BookStat(BaseModel):
    """按书聚合的同步情况（状态条的折叠明细）。

    `source` 用来区分内置书（`book`）与微信读书（`weread`）：两者的"已同步"含义不同，
    前者是用户划的词，后者是平台带过来的划线/想法。
    """

    book_id: str
    title: str = ""
    source: str = ""
    count: int = 0
    last_at: datetime | None = None


class KnowledgeStatsResponse(BaseModel):
    """知识库概览统计（前端仪表盘）。"""

    total: int
    by_source: dict[str, int] = Field(default_factory=dict)
    by_embed_status: dict[str, int] = Field(default_factory=dict)
    top_tags: list[TagCount] = Field(default_factory=list)
    # A 模块状态条：书级明细 / 上次同步时间 / 暂不可检索条数
    by_book: list[BookStat] = Field(default_factory=list)
    last_synced_at: datetime | None = None
    # pending + embed_failed：**代码算**，不让前端各自加一遍（两处各算必然分叉）
    not_ready: int = 0
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
    q: str | None = Query(default=None, max_length=100, description="关键词：标题或正文包含即命中"),
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> KnowledgeListResponse:
    """分页列出当前用户未删除条目（默认 20 条，最多 100 条）；传 `q` 则按关键词过滤。"""
    repo = KnowledgeRepository(session, user_id=user_id)
    total = repo.count_active(user_id, query=q)
    rows = repo.page_active(user_id, limit=limit, offset=offset, query=q)
    return KnowledgeListResponse(
        items=[
            KnowledgeBrief(
                item_id=r.id,
                title=r.title,
                snippet=r.snippet,
                tags=list(r.tags or []),
                read_progress=r.read_progress,
                embed_status=r.embed_status,
                source=r.source or "manual",
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
        request_id=trace.get_request_id() or "",
    )


@router.get("/stats", response_model=KnowledgeStatsResponse)
def get_stats(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
):
    """知识库概览统计（前端仪表盘用）：总数 / 来源分布 / 向量化状态 / 高频标签。

    纯本地聚合，不调模型。标签频次取前 15 个（出现次数降序、同频按名称排序）。
    另给三项供 A 模块「同步状态条」使用：`by_book`（书级明细）、`last_synced_at`、`not_ready`。
    """
    from collections import Counter

    from sqlalchemy import select

    from app.domain.models.book import Book
    from app.domain.models.knowledge_item import KnowledgeItem

    rows = session.execute(
        select(
            KnowledgeItem.source,
            KnowledgeItem.embed_status,
            KnowledgeItem.tags,
            KnowledgeItem.source_item_id,
            KnowledgeItem.source_locator,
            KnowledgeItem.created_at,
        ).where(
            KnowledgeItem.is_deleted.is_(False),
            KnowledgeItem.user_id == user_id,
        )
    ).all()

    by_source = Counter(r.source or "manual" for r in rows)
    by_embed = Counter(r.embed_status or "pending" for r in rows)
    tag_counter: Counter = Counter()
    for r in rows:
        tag_counter.update(t.strip() for t in (r.tags or []) if t and t.strip())
    top_tags = sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:15]

    book_buckets = _aggregate_by_book(rows)
    _fill_book_titles(session, user_id, book_buckets)

    last_synced_at = _last_weread_sync(session, user_id)

    return KnowledgeStatsResponse(
        total=len(rows),
        by_source=dict(by_source),
        by_embed_status=dict(by_embed),
        top_tags=[TagCount(tag=tag, count=count) for tag, count in top_tags],
        by_book=[
            BookStat(
                book_id=b["book_id"],
                title=b["title"],
                source=b["source"],
                count=b["count"],
                last_at=b["last_at"],
            )
            for b in sorted(book_buckets.values(), key=lambda x: (-x["count"], x["book_id"]))
        ],
        last_synced_at=last_synced_at,
        not_ready=int(by_embed.get("pending", 0)) + int(by_embed.get("embed_failed", 0)),
        request_id=trace.get_request_id() or "",
    )


def _aggregate_by_book(rows) -> dict[str, dict]:
    """按书聚合条目数。

    两种来源的书 id 存在不同位置，这里统一成 `{source}:{book_id}` 作键：
    - 内置书：`source_item_id` 就是 book_id（`books/service.add_note` 写入）
    - 微信读书：book_id 在 `source_locator.book_id` 里（`_collect_book::locator` 写入）

    `title` 先取 locator 里的书名（微信读书自带）；内置书留空，由 `_fill_book_titles` 补。
    """
    buckets: dict[str, dict] = {}
    for r in rows:
        book_id = ""
        title = ""
        if r.source == "book" and r.source_item_id:
            book_id = str(r.source_item_id)
        elif r.source == "weread":
            loc = r.source_locator or {}
            book_id = str(loc.get("book_id") or "")
            title = str(loc.get("book_title") or "")
        if not book_id:
            continue  # 手动记的条目不属于任何书，不计入书级明细
        key = f"{r.source}:{book_id}"
        bucket = buckets.setdefault(
            key,
            {"book_id": book_id, "title": title, "source": r.source, "count": 0, "last_at": None},
        )
        bucket["count"] += 1
        if r.created_at is not None and (bucket["last_at"] is None or r.created_at > bucket["last_at"]):
            bucket["last_at"] = r.created_at
    return buckets


def _fill_book_titles(session: Session, user_id: str, buckets: dict[str, dict]) -> None:
    """给内置书的书级明细补书名（微信读书自带书名，不查库）。"""
    from sqlalchemy import select

    from app.domain.models.book import Book

    ids = [b["book_id"] for b in buckets.values() if b["source"] == "book" and not b["title"]]
    if not ids:
        return
    titles = {
        b.id: b.title
        for b in session.scalars(
            select(Book).where(Book.user_id == user_id, Book.id.in_(ids))
        )
    }
    for bucket in buckets.values():
        if bucket["source"] == "book" and not bucket["title"]:
            bucket["title"] = titles.get(bucket["book_id"], "")


def _last_weread_sync(session: Session, user_id: str) -> datetime | None:
    """微信读书上次同步时间。读失败按"没有"处理——状态条少一个时间不影响主流程。"""
    try:
        from app.domain.repositories.learning_event_repository import LearningEventRepository
        from app.feedback import events

        rows = LearningEventRepository(session, user_id=user_id).list_recent(
            user_id, event_type=events.WEREAD_SYNCED, limit=1
        )
        return rows[0].occurred_at if rows else None
    except Exception:  # pragma: no cover - 防御性兜底
        return None


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
