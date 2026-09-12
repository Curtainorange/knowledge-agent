"""书籍仓储：上传 / 列表 / 进度 / 软删，强制 user_id 作用域。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.book import Book
from app.domain.repositories.base import BaseRepository


class BookRepository(BaseRepository[Book]):
    def __init__(self, session: Session, user_id: str | None = None):
        super().__init__(session, user_id)

    def create(
        self,
        *,
        user_id: str,
        title: str,
        author: str,
        format: str,
        file_path: str,
        chapters: list,
        full_text: str,
        total_chars: int,
    ) -> Book:
        book = Book(
            user_id=user_id,
            title=title,
            author=author,
            format=format,
            file_path=file_path,
            chapters=chapters,
            full_text=full_text,
            total_chars=total_chars,
        )
        self._session.add(book)
        self._session.flush()
        return book

    def get(self, book_id: str) -> Book | None:
        book = self._session.get(Book, book_id)
        if book is None:
            return None
        self._guard(book.user_id)
        return book

    def list_active(self, user_id: str) -> list[Book]:
        self._guard(user_id)
        stmt = (
            select(Book)
            .where(Book.user_id == user_id, Book.is_deleted.is_(False))
            .order_by(Book.updated_at.desc())
        )
        return list(self._session.scalars(stmt))

    def update_progress(self, book: Book, current_char: int, read_progress: float) -> None:
        book.current_char = max(0, int(current_char))
        book.read_progress = max(0.0, min(1.0, float(read_progress)))
        self._session.flush()

    def soft_delete(self, book_id: str) -> Book | None:
        book = self.get(book_id)
        if book is None:
            return None
        book.is_deleted = True
        self._session.flush()
        return book
