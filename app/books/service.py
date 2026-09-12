"""书籍业务服务：上传（存文件 + 解析入库）、进度、划词存知识。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.books import parser
from app.core.config import settings
from app.domain.models.book import Book
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.book_repository import BookRepository
from app.ingestion.service import IngestionService
from app.retrieval.embedding import build_embedding

from pathlib import Path


def _books_root() -> Path:
    root = Path(settings.books_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _user_dir(user_id: str) -> Path:
    directory = _books_root() / user_id
    directory.mkdir(parents=True, exist_ok=True)
    return directory


class BookService:
    def __init__(self, session: Session):
        self._session = session

    def upload(self, *, user_id: str, filename: str, content: bytes) -> Book:
        repo = BookRepository(self._session, user_id=user_id)
        book_id = str(uuid4())
        suffix = Path(filename).suffix.lower()
        book_format = "epub" if suffix == ".epub" else "txt"
        dest = _user_dir(user_id) / f"{book_id}{suffix}"
        dest.write_bytes(content)

        parsed = (
            parser.parse_epub(dest)
            if book_format == "epub"
            else parser.parse_txt(dest, title=Path(filename).stem)
        )

        book = repo.create(
            user_id=user_id,
            title=parsed.title or Path(filename).stem,
            author=parsed.author,
            format=book_format,
            file_path=str(dest),
            chapters=parsed.chapters,
            full_text=parsed.full_text,
            total_chars=len(parsed.full_text),
        )
        self._session.commit()
        return book

    def update_progress(
        self, *, user_id: str, book_id: str, current_char: int, read_progress: float
    ) -> Book | None:
        repo = BookRepository(self._session, user_id=user_id)
        book = repo.get(book_id)
        if book is None:
            return None
        repo.update_progress(book, current_char=current_char, read_progress=read_progress)
        self._session.commit()
        return book

    def delete(self, *, user_id: str, book_id: str) -> Book | None:
        repo = BookRepository(self._session, user_id=user_id)
        book = repo.soft_delete(book_id)
        self._session.commit()
        return book

    def add_note(
        self,
        *,
        user_id: str,
        book_id: str,
        text: str,
        chapter_index: int | None = None,
    ) -> KnowledgeItem | None:
        """把阅读时划选的一段文字存为知识条目（source=book）。

        复用 IngestionService 走与手动录入完全相同的「先落库、再向量化」链路，
        因此这段摘录会直接进入语义检索，可被 L1 挖掘命中。
        """
        repo = BookRepository(self._session, user_id=user_id)
        book = repo.get(book_id)
        if book is None:
            return None

        chapter_title = ""
        if chapter_index is not None and book.chapters:
            for chapter in book.chapters:
                if chapter.get("index") == chapter_index:
                    chapter_title = chapter.get("title", "")
                    break
        title = f"{book.title} · {chapter_title}".strip(" ·") if chapter_title else book.title

        svc = IngestionService(self._session, build_embedding())
        item = svc.add_knowledge(
            user_id=user_id,
            title=title[:256],
            content=text.strip(),
            source="book",
            tags=["书籍摘录"],
        )
        item.source_item_id = book_id  # 关联回原书，便于溯源
        self._session.commit()
        return item
