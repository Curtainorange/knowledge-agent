"""一次性工具：给存量书籍建原文分块索引（供「就书讨论」检索原句）。

新书在上传时就会建块（`BookService.upload` → `chunking.rebuild`），这个脚本只补
0023 迁移之前入库的老书。幂等：已有块的书跳过；要重算加 `--force`。

跑法：
    python scripts/backfill_book_chunks.py                 # 只补缺失的块
    python scripts/backfill_book_chunks.py --force         # 全部重建
    python scripts/backfill_book_chunks.py --search "一万小时定律" --book 刻意练习
    python scripts/backfill_book_chunks.py --search "..." --book 刻意练习 --show 3

`--search` 用来人工核对检索质量：打印命中片段的章节、页码、分数与开头原文。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.books import chunking  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.domain.models.book import Book  # noqa: E402
from app.domain.models.book_chunk import BookChunk  # noqa: E402


def _open_session():
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)()


def _books(session, only: str = "") -> list[Book]:
    books = list(
        session.scalars(
            select(Book).where(Book.is_deleted.is_(False)).order_by(Book.created_at)
        )
    )
    if only:
        books = [b for b in books if only in b.title]
    return books


def _count(session, book_id: str) -> int:
    return len(
        list(session.scalars(select(BookChunk.id).where(BookChunk.book_id == book_id)))
    )


def backfill(session, *, force: bool) -> None:
    books = _books(session)
    if not books:
        print("书架是空的，没有可建的索引。")
        return

    print(f"{'书名':<28}{'字符数':>9}{'块数':>7}{'均块长':>8}  状态")
    print("-" * 74)
    for book in books:
        before = _count(session, book.id)
        if before and not force:
            avg = (book.total_chars // before) if before else 0
            print(f"{book.title[:26]:<28}{book.total_chars:>9}{before:>7}{avg:>8}  已存在，跳过")
            continue
        count = chunking.rebuild(session, book)
        session.commit()
        avg = (book.total_chars // count) if count else 0
        print(f"{book.title[:26]:<28}{book.total_chars:>9}{count:>7}{avg:>8}  已建（原 {before} 块）")
    print("-" * 74)


def probe(session, *, book_title: str, query: str, show: int) -> None:
    books = _books(session, only=book_title)
    if not books:
        print(f"书架上找不到含「{book_title}」的书。")
        return
    book = books[0]
    print(f"检索书：{book.title}（{book.total_chars} 字，{_count(session, book.id)} 块）")
    print(f"问题：{query}\n")
    hits = chunking.search(session, book=book, query=query, top_k=show)
    if not hits:
        print("没有命中任何片段（调用方此时只用通读笔记回答）。")
        return
    for i, hit in enumerate(hits, 1):
        where = " · ".join(
            str(x) for x in (hit.get("chapter_title"), hit.get("page")) if x
        )
        preview = (hit.get("text") or "").replace("\n", " ")[:70]
        print(f"[{i}] score={hit['score']}  {where}")
        print(f"    {preview}…")
        print(f"    char [{hit['char_start']}, {hit['char_end']})")


def main() -> None:
    parser = argparse.ArgumentParser(description="书籍原文分块索引回填 / 检索自检")
    parser.add_argument("--force", action="store_true", help="已有块也重建")
    parser.add_argument("--search", default="", help="检索自检：要查的问题")
    parser.add_argument("--book", default="", help="检索自检：书名关键词")
    parser.add_argument("--show", type=int, default=chunking.SEARCH_TOP_K, help="展示片段数")
    args = parser.parse_args()

    session = _open_session()
    try:
        if args.search and args.book:
            probe(session, book_title=args.book, query=args.search, show=args.show)
            return
        backfill(session, force=args.force)
        if args.search:
            print("\n提示：检索自检需要同时给 --book，例如 --book 刻意练习")
    finally:
        session.close()


if __name__ == "__main__":
    main()
