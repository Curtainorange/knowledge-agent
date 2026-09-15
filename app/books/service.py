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

        # 插图落盘：存到 <books_dir>/<user>/<book_id>/images/<name>，
        # 阅读器按 full_text 里的 [[IMG:name]] 占位符回填显示
        if parsed.images:
            images_dir = _user_dir(user_id) / book_id / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            for img in parsed.images:
                (images_dir / img["name"]).write_bytes(img["data"])

        book = repo.create(
            book_id=book_id,
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

        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.BOOK_UPLOADED,
            payload={
                "book_id": book.id, "format": book_format,
                "total_chars": book.total_chars, "chapter_count": len(parsed.chapters),
            },
        )
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

        from app.feedback import events

        # 阅读行为是 L4 偏离检测与 L5 归因的核心信号，务必逐次记录
        events.record(
            self._session, user_id=user_id, event_type=events.BOOK_PROGRESS,
            payload={
                "book_id": book_id, "current_char": book.current_char,
                "read_progress": book.read_progress,
            },
        )
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

        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.NOTE_CREATED,
            payload={
                "item_id": item.id, "book_id": book_id,
                "chapter_index": chapter_index, "has_thought": bool(thought),
                "excerpt_len": len(excerpt),
            },
        )
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

    def _titles_for(self, user_id: str, book_ids: list[str]) -> dict[str, str]:
        if not book_ids:
            return {}
        stmt = select(Book).where(Book.user_id == user_id, Book.id.in_(book_ids))
        return {b.id: b.title for b in self._session.scalars(stmt)}

    def reading_log(self, *, user_id: str, days: int = 30, group: str = "day") -> dict:
        """按天 / 周 / 月聚合阅读记录：每个时间桶读了哪几本书、各读了多少字。

        - `group`：`day`（默认）/ `week`（ISO 周）/ `month`。
        - 数据源是 BOOK_PROGRESS 事件（每次保存进度都追加一条，含 book_id 与
          current_char）。**桶内阅读量 = 桶内达到的最大已读位置 − 此前位置**；
          用「最大」而非「最后一次」——读者回翻时最后一条会退回较低位置，
          按最后一次算会把当桶的阅读量抹掉。
        - 跨桶基准用「此前所有桶的最大位置」，因此跨天/跨周连续阅读也不会漏算；
          桶排序按桶内最新日期，避免 ISO 周跨年时字符串序错乱。
        """
        from datetime import datetime, timedelta, timezone

        from app.domain.repositories.learning_event_repository import LearningEventRepository
        from app.feedback import events as event_types

        group = group if group in ("day", "week", "month") else "day"
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=max(1, days))
        rows = LearningEventRepository(self._session).list_since(
            user_id, since=since, event_type=event_types.BOOK_PROGRESS
        )

        def bucket_key(day) -> str:
            if group == "week":
                iso = day.isocalendar()
                return f"{iso[0]}-W{iso[1]:02d}"
            if group == "month":
                return f"{day.year}-{day.month:02d}"
            return day.isoformat()

        # 每本书每个桶达到的最大已读位置（附该位置对应的进度）
        per_book: dict[str, dict[str, dict]] = {}
        bucket_latest: dict[str, str] = {}  # 桶 -> 桶内最新日期（仅用于排序）
        for row in rows:
            payload = row.payload or {}
            book_id = payload.get("book_id")
            if not book_id:
                continue
            occurred = row.occurred_at.date()
            key = bucket_key(occurred)
            bucket_latest[key] = max(bucket_latest.get(key, ""), occurred.isoformat())
            slot = per_book.setdefault(book_id, {}).setdefault(
                key, {"max_char": -1, "progress": 0.0}
            )
            char = int(payload.get("current_char") or 0)
            if char > slot["max_char"]:
                slot["max_char"] = char
                slot["progress"] = float(payload.get("read_progress") or 0.0)

        titles = self._titles_for(user_id, list(per_book))
        order = lambda key: bucket_latest.get(key, key)  # noqa: E731

        buckets: dict[str, dict] = {}
        for book_id, by_bucket in per_book.items():
            prev_max = 0
            for key in sorted(by_bucket, key=order):
                peak = by_bucket[key]["max_char"]
                delta = max(0, peak - prev_max)
                prev_max = max(prev_max, peak)
                if delta <= 0:
                    continue
                bucket = buckets.setdefault(key, {"key": key, "total_chars": 0, "books": []})
                bucket["books"].append({
                    "book_id": book_id,
                    "title": titles.get(book_id, "（已删除）"),
                    "chars_read": delta,
                    "progress": round(by_bucket[key]["progress"], 4),
                })
                bucket["total_chars"] += delta

        ordered = [buckets[k] for k in sorted(buckets, key=order, reverse=True)]
        return {
            "group": group,
            "buckets": ordered,
            "total_chars": sum(b["total_chars"] for b in ordered),
            "active_buckets": len(ordered),
        }
