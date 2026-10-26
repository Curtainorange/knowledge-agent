"""重新解析已入库的书（修数据用）。

用途：解析器修好之后，把「当时解析错了」的书按新逻辑重读一遍——
补回丢失的插图、清掉混进正文的 `<title>未知</title>`、重建文本分块索引。

默认 dry-run（只打印、不写库）；确认无误后加 `--write` 才落库。

    python scripts/repair_book_parse.py --book-id <id>
    python scripts/repair_book_parse.py --book-id <id> --write

注意：
- 阅读进度按「字符比例」换算保留（正文长度会变，绝对值不能直接沿用）；
- 章节边界会随插图占位符变化，该书上的划线笔记定位可能偏移。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.books import chunking  # noqa: E402
from app.books.service import _PARSERS  # noqa: E402
from app.domain.db import SessionLocal  # noqa: E402
from app.domain.models.book import Book  # noqa: E402

_IMAGE_TAG = re.compile(r"\[\[IMG:[^\]]+\]\]")


def _resolve_source(book: Book) -> Path:
    path = Path(book.file_path.replace("\\", "/"))
    return path if path.is_absolute() else ROOT / path


def repair(book_id: str, *, write: bool) -> int:
    with SessionLocal() as session:
        book = session.get(Book, book_id)
        if book is None:
            print(f"[x] 没有这本书：{book_id}")
            return 2
        source = _resolve_source(book)
        if not source.exists():
            print(f"[x] 原文件不存在：{source}")
            return 2

        parsed = _PARSERS.get(book.format, _PARSERS["epub"])(source, title=book.title)

        old_chars = book.total_chars or 1
        old_clean = len(_IMAGE_TAG.sub("", book.full_text or "").strip())
        new_clean = len(_IMAGE_TAG.sub("", parsed.full_text or "").strip())
        ratio = (book.current_char or 0) / old_chars
        new_current = min(int(round(ratio * len(parsed.full_text))), len(parsed.full_text))
        old_images = len(list((source.parent / book.id / "images").glob("*"))) if (
            source.parent / book.id / "images"
        ).is_dir() else 0

        print(f"书名：{book.title}")
        print(f"  正文长度     {book.total_chars} → {len(parsed.full_text)}")
        print(f"  有效文字     {old_clean} → {new_clean}")
        print(f"  插图         {old_images} → {len(parsed.images)}")
        print(f"  章节         {len(book.chapters or [])} → {len(parsed.chapters)}")
        print(f"  进度         {book.read_progress:.0%}（当前字符 {book.current_char} → {new_current}）")
        print(f"  「未知」     {(book.full_text or '').count('未知')} → {(parsed.full_text or '').count('未知')}")

        if not write:
            print("\n[dry-run] 未写库。加 --write 执行。")
            return 0

        images_dir = source.parent / book.id / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        for img in parsed.images:
            (images_dir / img["name"]).write_bytes(img["data"])

        book.full_text = parsed.full_text
        book.chapters = parsed.chapters
        book.total_chars = len(parsed.full_text)
        book.current_char = new_current
        book.author = parsed.author or book.author
        session.commit()

        try:
            count = chunking.rebuild(session, book)
            session.commit()
            print(f"\n[ok] 已写库：{len(parsed.images)} 张插图，{count} 个文本分块。")
        except Exception as exc:  # noqa: BLE001 索引是增强项，失败不回滚正文
            session.rollback()
            print(f"\n[!] 正文已写库，但分块重建失败：{exc}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按新解析逻辑重读已入库的书")
    ap.add_argument("--book-id", required=True)
    ap.add_argument("--write", action="store_true", help="真正写库（默认只打印）")
    args = ap.parse_args()
    return repair(args.book_id, write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
