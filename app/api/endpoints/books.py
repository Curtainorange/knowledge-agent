"""书籍端点（阅读器）：上传 / 书架 / 详情 / 章节 / 进度 / 划词存知识。

上传采用「raw body + X-Filename 头」而非 multipart，避免引入 python-multipart 依赖；
文件名走 URL 编码以支持中文。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.deps import get_session, get_user_id
from app.books.service import BookService
from app.core import trace
from app.domain.models.book import Book
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.book_repository import BookRepository

router = APIRouter(prefix="/api/v1/books", tags=["books"])

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB
_ALLOWED_SUFFIXES = (".txt", ".epub")


class ChapterOut(BaseModel):
    index: int
    title: str
    char_start: int
    char_end: int


class BookOut(BaseModel):
    book_id: str
    title: str
    author: str
    format: str
    total_chars: int
    chapter_count: int
    read_progress: float
    current_char: int
    created_at: datetime


class BookListResponse(BaseModel):
    items: list[BookOut]
    total: int
    request_id: str


class BookDetailResponse(BaseModel):
    book_id: str
    title: str
    author: str
    format: str
    total_chars: int
    chapters: list[ChapterOut]
    read_progress: float
    current_char: int
    request_id: str


class ChapterContentResponse(BaseModel):
    book_id: str
    index: int
    title: str
    content: str
    # 该章在全文中的字符区间：前端据此把「选中位置」换算成全文偏移，用于高亮与回溯
    char_start: int = 0
    char_end: int = 0
    next_index: int | None
    request_id: str


class ProgressRequest(BaseModel):
    current_char: int = Field(ge=0)
    read_progress: float = Field(ge=0.0, le=1.0)


class NoteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    note: str = Field(default="", max_length=2000, description="自己的想法（可空）")
    chapter_index: int | None = None
    char_start: int | None = Field(default=None, ge=0)
    char_end: int | None = Field(default=None, ge=0)


class NoteResponse(BaseModel):
    item_id: str
    embed_status: str
    has_note: bool = False
    request_id: str


class NoteItemOut(BaseModel):
    item_id: str
    excerpt: str  # 摘录原文（不含自己的想法）
    note: str = ""
    chapter_index: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    created_at: datetime


class NotesResponse(BaseModel):
    items: list[NoteItemOut]
    total: int
    request_id: str


class DeleteResponse(BaseModel):
    book_id: str
    deleted: bool
    request_id: str


def _book_out(book: Book) -> BookOut:
    return BookOut(
        book_id=book.id,
        title=book.title,
        author=book.author,
        format=book.format,
        total_chars=book.total_chars,
        chapter_count=len(book.chapters or []),
        read_progress=book.read_progress or 0.0,
        current_char=book.current_char or 0,
        created_at=book.created_at,
    )


def _repo(user_id: str, session: Session) -> BookRepository:
    return BookRepository(session, user_id=user_id)


@router.post("", response_model=BookOut, status_code=status.HTTP_201_CREATED)
async def upload_book(
    request: Request,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> BookOut:
    filename = unquote(request.headers.get("X-Filename", "book.txt"))
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .txt 或 .epub 格式")

    content = await request.body()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件过大（上限 50MB）")

    try:
        book = BookService(session).upload(user_id=user_id, filename=filename, content=content)
    except Exception as exc:  # 解析失败等
        raise HTTPException(status_code=400, detail=f"解析失败：{exc}") from exc
    return _book_out(book)


@router.get("", response_model=BookListResponse)
def list_books(
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> BookListResponse:
    books = _repo(user_id, session).list_active(user_id)
    return BookListResponse(
        items=[_book_out(book) for book in books],
        total=len(books),
        request_id=trace.get_request_id() or "",
    )


@router.get("/{book_id}", response_model=BookDetailResponse)
def get_book(
    book_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> BookDetailResponse:
    try:
        book = _repo(user_id, session).get(book_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    if book is None or book.is_deleted:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return BookDetailResponse(
        book_id=book.id,
        title=book.title,
        author=book.author,
        format=book.format,
        total_chars=book.total_chars,
        chapters=[ChapterOut(**chapter) for chapter in (book.chapters or [])],
        read_progress=book.read_progress or 0.0,
        current_char=book.current_char or 0,
        request_id=trace.get_request_id() or "",
    )


@router.get("/{book_id}/chapter/{index}", response_model=ChapterContentResponse)
def get_chapter(
    book_id: str,
    index: int,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> ChapterContentResponse:
    try:
        book = _repo(user_id, session).get(book_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    if book is None or book.is_deleted:
        raise HTTPException(status_code=404, detail="书籍不存在")

    chapter = next((c for c in (book.chapters or []) if c.get("index") == index), None)
    if chapter is None:
        raise HTTPException(status_code=404, detail="章节不存在")

    ordered = sorted(book.chapters or [], key=lambda c: c.get("index", 0))
    pos = ordered.index(chapter)
    next_index = ordered[pos + 1].get("index") if pos + 1 < len(ordered) else None

    content = book.full_text[chapter["char_start"] : chapter["char_end"]]
    return ChapterContentResponse(
        book_id=book.id,
        index=chapter["index"],
        title=chapter["title"],
        content=content,
        char_start=chapter["char_start"],
        char_end=chapter["char_end"],
        next_index=next_index,
        request_id=trace.get_request_id() or "",
    )


@router.patch("/{book_id}/progress", response_model=BookOut)
def update_progress(
    book_id: str,
    body: ProgressRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> BookOut:
    try:
        book = BookService(session).update_progress(
            user_id=user_id,
            book_id=book_id,
            current_char=body.current_char,
            read_progress=body.read_progress,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    if book is None or book.is_deleted:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return _book_out(book)


@router.delete("/{book_id}", response_model=DeleteResponse)
def delete_book(
    book_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> DeleteResponse:
    try:
        book = BookService(session).delete(user_id=user_id, book_id=book_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    if book is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return DeleteResponse(book_id=book.id, deleted=True, request_id=trace.get_request_id() or "")


def _excerpt(item: KnowledgeItem) -> str:
    """取回摘录原文（去掉拼接进 raw_content 的想法部分）。"""
    content = item.raw_content or ""
    marker = "\n\n【我的想法】"
    return content.split(marker, 1)[0] if marker in content else content


@router.post("/{book_id}/notes", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
def add_note(
    book_id: str,
    body: NoteRequest,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> NoteResponse:
    """阅读时划选一段文字存为知识；可附带自己的想法。"""
    try:
        item = BookService(session).add_note(
            user_id=user_id,
            book_id=book_id,
            text=body.text,
            note=body.note,
            chapter_index=body.chapter_index,
            char_start=body.char_start,
            char_end=body.char_end,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    if item is None:
        raise HTTPException(status_code=404, detail="书籍不存在")
    return NoteResponse(
        item_id=item.id,
        embed_status=item.embed_status,
        has_note=bool(item.note),
        request_id=trace.get_request_id() or "",
    )


@router.get("/{book_id}/notes", response_model=NotesResponse)
def list_notes(
    book_id: str,
    user_id: str = Depends(get_user_id),
    session: Session = Depends(get_session),
) -> NotesResponse:
    """本书已录入的知识（含原文位置），供阅读页高亮与「本书知识」面板使用。"""
    try:
        items = BookService(session).list_notes(user_id=user_id, book_id=book_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="无权访问该书籍") from exc
    return NotesResponse(
        items=[
            NoteItemOut(
                item_id=item.id,
                excerpt=_excerpt(item),
                note=item.note or "",
                chapter_index=(item.source_locator or {}).get("chapter_index"),
                char_start=(item.source_locator or {}).get("char_start"),
                char_end=(item.source_locator or {}).get("char_end"),
                created_at=item.created_at,
            )
            for item in items
        ],
        total=len(items),
        request_id=trace.get_request_id() or "",
    )
