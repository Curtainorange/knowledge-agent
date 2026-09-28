"""智能体通读记录仓储（user_id 强制过滤，与项目铁律一致）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.book_agent_reading import BookAgentReading


class BookAgentReadingRepository:
    def __init__(self, session: Session, user_id: str = ""):
        self._session = session
        self._user_id = user_id

    def _guard(self, reading: BookAgentReading | None) -> BookAgentReading | None:
        """越权一律当不存在（与其它仓储同一套守卫口径）。"""
        if reading is None:
            return None
        if self._user_id and reading.user_id != self._user_id:
            raise PermissionError("无权访问该通读记录")
        return reading

    def get(self, reading_id: str) -> BookAgentReading | None:
        row = self._session.get(BookAgentReading, reading_id)
        return self._guard(row)

    def get_by_book(self, book_id: str) -> BookAgentReading | None:
        """该书当前生效的通读记录（每对 user/book 只留最新一份）。"""
        stmt = (
            select(BookAgentReading)
            .where(
                BookAgentReading.user_id == self._user_id,
                BookAgentReading.book_id == book_id,
                BookAgentReading.is_deleted.is_(False),
            )
            .order_by(BookAgentReading.created_at.desc(), BookAgentReading.id.desc())
            .limit(1)
        )
        return self._guard(self._session.scalars(stmt).first())

    def upsert(
        self,
        *,
        book_id: str,
        status: str,
        total_chars: int,
        summary: str,
        chapters_note: list[dict],
        chunk_count: int,
        failed_chunks: int,
        truncated: bool,
    ) -> BookAgentReading:
        """覆盖式写入：已有记录就地改字段（id 不变），没有就新建。"""
        reading = self.get_by_book(book_id)
        if reading is None:
            reading = BookAgentReading(user_id=self._user_id, book_id=book_id)
            self._session.add(reading)
        reading.status = status
        reading.total_chars = int(total_chars)
        reading.summary = summary
        reading.chapters_note = chapters_note
        reading.chunk_count = int(chunk_count)
        reading.failed_chunks = int(failed_chunks)
        reading.truncated = bool(truncated)
        self._session.flush()
        return reading
