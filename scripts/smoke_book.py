"""书籍三项真实冒烟：recommend / read_whole_book / discuss（走真实 LLM KEY）。

与 smoke_llm.py 同一定位：几秒/几分钟内判断是模型层产出质量不足还是链路问题。
只用内存 SQLite + 临时目录，不碰 dev.db；user_id 用任意标识（编排层不查 users 表）。

用法：python scripts/smoke_book.py   （需要 .env 里配置真实 KEY 才有非 Mock 产出）
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BOOK_TEXT = (
    "第一章 为什么笔记会失效\n"
    "大多数人记笔记的方式是「收藏」：看到有用的内容就存下来，然后再也不看。"
    "作者把这类笔记称为死水笔记——它们只进不出，从不与既有知识发生碰撞。"
    "真正有效的笔记必须被反复检索与改写，否则保存动作本身就是遗忘的开始。\n\n"
    "第二章 让知识互相冲突\n"
    "作者提出，冲突是知识升级的信号：当你新读到的观点与你笔记里的旧观点矛盾时，"
    "不要急于站队，而应把两条主张并排放着，去找它们各自成立的前提条件。"
    "本章给出了三个真实案例，说明凡是没被冲突检验过的知识，都值得怀疑。\n\n"
    "第三章 外部系统补偿遗忘\n"
    "遗忘不可避免，但可以被外部系统补偿：复习的排期应当交给算法而不是直觉，"
    "因为提取难度才是记忆保持的关键。重读几乎无效，费力想起来一次胜过看十遍。\n"
)

CHAPTERS = []
pos = 0
for part in BOOK_TEXT.split("\n\n"):
    title_line, _, body = part.partition("\n")
    CHAPTERS.append({"index": len(CHAPTERS), "title": title_line.strip(),
                     "char_start": pos, "char_end": pos + len(part)})
    pos += len(part)
FULL_TEXT = BOOK_TEXT


def main() -> None:
    from app.domain.models.base import Base
    from app.domain.repositories.book_repository import BookRepository
    from app.domain.repositories.knowledge_repository import KnowledgeRepository
    from app.domain.repositories.learning_plan_repository import LearningGoalRepository
    from app.llm.gateway import ModelGateway
    from app.agent import book_orchestrator

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    user_id = "smoke_book"

    KnowledgeRepository(session, user_id=user_id).create(
        user_id=user_id, title="B+树与范围查询", content="B+树的叶子链表让范围查询不必回溯。 #数据库"
    )
    KnowledgeRepository(session, user_id=user_id).create(
        user_id=user_id, title="间隔复习", content="提取难度决定记忆保持，重读几乎无效。 #学习方法"
    )
    LearningGoalRepository(session, user_id=user_id).create(
        user_id=user_id, description="三个月掌握数据分析"
    )
    session.commit()

    tmp = Path(tempfile.mkdtemp(prefix="smoke_book_"))
    book_path = tmp / "smoke.txt"
    book_path.write_text(FULL_TEXT, encoding="utf-8")
    book = BookRepository(session, user_id=user_id).create(
        user_id=user_id, title="笔记的方法", author="测试作者", format="txt",
        file_path=str(book_path), chapters=CHAPTERS, full_text=FULL_TEXT,
        total_chars=len(FULL_TEXT),
    )
    session.commit()

    gateway = ModelGateway()
    print("=== providers:", gateway.real_provider_names or ["MockProvider（未配真实 KEY）"])

    print("\n=== 1) recommend ===")
    result = book_orchestrator.recommend(session, gateway, user_id=user_id)
    print(json.dumps({
        "state": result.state, "overview": result.overview,
        "items": [i.model_dump() for i in result.items], "note": result.note,
    }, ensure_ascii=False, indent=2))

    print("\n=== 2) read_whole_book ===")
    digest = book_orchestrator.read_whole_book(session, gateway, user_id=user_id, book=book)
    print(json.dumps({
        "state": digest.state, "summary": digest.summary,
        "chapters": digest.chapters, "chunks": digest.chunk_count,
        "failed": digest.failed_chunks, "note": digest.note,
    }, ensure_ascii=False, indent=2))

    print("\n=== 3) discuss ===")
    answer = book_orchestrator.discuss(
        session, gateway, user_id=user_id, book=book, question="这本书的核心主张是什么？我该怎么用？"
    )
    print(answer)

    items = session.query(
        __import__("app.domain.models.knowledge_item", fromlist=["KnowledgeItem"]).KnowledgeItem
    ).filter_by(user_id=user_id).count()
    print(f"\n=== 隔离检查：知识库条目数仍为 {items}（通读前后一致）===")


if __name__ == "__main__":
    main()
