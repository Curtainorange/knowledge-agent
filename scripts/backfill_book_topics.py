"""一次性工具：给存量通读笔记的 chapters_note 补 topic 领域标签。

背景：BOOK_DIGEST_CHUNK v2 起通读时顺手产出 topic；v1 时期通读的书没有标签，
影子主张只能回退书名 topic、走不进主题通道。本脚本把每本书的分章要点
批量送一次模型打标签（一本书一次调用），避免整本重读。

用完即弃：新通读的书自带 topic，无需再跑。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.domain.models.book import Book
from app.domain.models.book_agent_reading import BookAgentReading
from app.llm.gateway import ModelGateway
from app.llm.structure import parse_structured


class ChapterTopics(BaseModel):
    """按 index 给每章打领域标签。"""

    topics: list[dict] = Field(default_factory=list)


def main() -> None:
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()

    readings = list(session.scalars(
        select(BookAgentReading).where(
            BookAgentReading.status == "done",
            BookAgentReading.is_deleted.is_(False),
        )
    ))
    books = {
        b.id: b
        for b in session.scalars(select(Book).where(Book.id.in_([r.book_id for r in readings])))
    } if readings else {}

    gateway = ModelGateway()
    for reading in readings:
        book = books.get(reading.book_id)
        if book is None:
            continue
        chapters = [c for c in (reading.chapters_note or []) if c.get("gist")]
        if not chapters:
            continue
        listing = "\n".join(
            f"{c['index']}. 【{c.get('title', '')}】{c.get('gist', '')}" for c in chapters
        )
        completion = gateway.chat(
            task_type="book_digest",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "给一本书各章的要点概括打领域主题标签（2-6 字通用领域词，"
                        "如 学习方法 / 时间管理 / 心理咨询 / 成功观，不用书名）。"
                        '输出严格 JSON：{"topics":[{"index":0,"topic":"..."}]}，'
                        "index 必须与输入一一对应。"
                    ),
                },
                {"role": "user", "content": f"书名：《{book.title}》\n\n{listing}"},
            ],
            user_id=reading.user_id,
            json_model=ChapterTopics,
        )
        data = parse_structured(completion.text, validator=lambda d: ChapterTopics(**d))
        topic_by_index = {t.get("index"): str(t.get("topic", "")).strip()[:64] for t in data.topics}

        updated = []
        for c in reading.chapters_note or []:
            row = dict(c)
            row["topic"] = topic_by_index.get(c.get("index"), "")
            updated.append(row)
        reading.chapters_note = updated
        session.commit()
        print(f"《{book.title}》已补 {sum(1 for v in topic_by_index.values() if v)} 个标签")

    print("done")


if __name__ == "__main__":
    main()
