"""一次性演示：对书架里的真实书籍执行智能体通读（默认《世界尽头的咖啡馆》）。

直接连 dev.db（只写 book_agent_readings 独立表，不碰用户阅读进度与知识库），
走真实 LLM KEY；跑完打印总评与分章要点概要。

用法：python scripts/read_demo_book.py [书名]
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

TITLE = sys.argv[1] if len(sys.argv) > 1 else "世界尽头的咖啡馆"


def main() -> None:
    from app.agent import book_orchestrator
    from app.domain.repositories.book_agent_reading_repository import BookAgentReadingRepository
    from app.llm.gateway import ModelGateway

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()

    # 仓储层强制 user_id 过滤，而脚本场景还不知道书属于哪个用户——
    # 先按标题全库定位（只读），拿到归属后再走正常编排层
    from sqlalchemy import select

    from app.domain.models.book import Book

    book = session.scalars(
        select(Book).where(Book.title.contains(TITLE), Book.is_deleted.is_(False))
    ).first()
    if book is None:
        raise SystemExit(f"书架里没找到《{TITLE}》")

    user_id = book.user_id
    print(f"书：《{book.title}》 作者：{book.author or '未知'} 全文 {book.total_chars} 字")
    print(f"用户阅读进度（通读前）：{round(float(book.read_progress or 0.0) * 100, 1)}%")

    gateway = ModelGateway()
    print("providers:", gateway.real_provider_names or ["MockProvider（未配真实 KEY）"])
    print("开始通读……", flush=True)

    outcome = book_orchestrator.read_whole_book(
        session, gateway, user_id=user_id, book=book, force=True
    )
    session.commit()

    print(json.dumps({
        "state": outcome.state,
        "chunks": outcome.chunk_count,
        "failed_chunks": outcome.failed_chunks,
        "truncated": outcome.truncated,
        "summary": outcome.summary,
        "chapters": outcome.chapters,
        "note": outcome.note,
    }, ensure_ascii=False, indent=2))

    fresh = session.get(type(book), book.id)
    print(f"用户阅读进度（通读后）：{round(float(fresh.read_progress or 0.0) * 100, 1)}%（应与通读前一致）")
    reading = BookAgentReadingRepository(session, user_id=user_id).get_by_book(book.id)
    print(f"通读记录：status={reading.status} chunks={reading.chunk_count} failed={reading.failed_chunks}")


if __name__ == "__main__":
    main()
