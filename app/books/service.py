"""书籍业务服务：上传（存文件 + 解析入库）、进度、划词存知识。"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
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
        note: str = "",
        chapter_index: int | None = None,
        char_start: int | None = None,
        char_end: int | None = None,
    ) -> KnowledgeItem | None:
        """把阅读时划选的一段文字（可附带自己的想法）存为知识条目。

        - 摘录原文进 `raw_content`，自己的想法进 `note`
        - 想法**同时拼进** `raw_content`：这样它能被语义检索命中，
          L1 挖掘到的是「你的理解」，而不只是原文
        - `source_locator` 记录原文位置，供阅读页高亮与跳回原文
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

        excerpt = text.strip()
        thought = (note or "").strip()
        content = excerpt if not thought else f"{excerpt}\n\n【我的想法】{thought}"

        svc = IngestionService(self._session, build_embedding())
        item = svc.add_knowledge(
            user_id=user_id,
            title=title[:256],
            content=content,
            source="book",
            tags=["书籍摘录"] if not thought else ["书籍摘录", "读书笔记"],
        )
        item.source_item_id = book_id
        item.note = thought
        if char_start is not None and char_end is not None:
            item.source_locator = {
                "chapter_index": chapter_index,
                "char_start": int(char_start),
                "char_end": int(char_end),
            }
        self._session.commit()
        return item

    def list_notes(self, *, user_id: str, book_id: str) -> list[KnowledgeItem]:
        """本书已录入的知识，按原文位置排序。

        供阅读页做「已录区间高亮」与「本书知识面板」。
        """
        repo = BookRepository(self._session, user_id=user_id)
        book = repo.get(book_id)
        if book is None:
            return []
        stmt = select(KnowledgeItem).where(
            KnowledgeItem.user_id == user_id,
            KnowledgeItem.source == "book",
            KnowledgeItem.source_item_id == book_id,
            KnowledgeItem.is_deleted.is_(False),
        )
        items = list(self._session.scalars(stmt))
        items.sort(key=lambda i: (i.source_locator or {}).get("char_start", 0))
        return items
