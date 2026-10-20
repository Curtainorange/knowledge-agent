"""真机冒烟：四条新模型链路（B / C / D / E 各一条）。

## 为什么单独一个脚本

`pytest` 全程 Mock，只能证明"逻辑对"，证明不了"模型给的东西能用"。
这条脚本用**真实 KEY** 跑一遍四个生成入口，并用**代码侧校验函数**判断产出是否合格——
也就是生产路径上真正会拒掉什么，这里就会看到什么。

## 为什么不碰 dev.db

用独立的 `smoke_modules.db`（跑完删掉），造最小数据。
用户库里有真实笔记，冒烟不该往里写测试数据。

## 四条链路

| 链路 | 验什么 |
|---|---|
| reading_suggestion | 依据能不能被引用、每条是否带 basis 与验证方式 |
| conflict_attribution | 七项是否齐、背景是否写成可读的判断（不是"背景不同"） |
| probe_question | 三个选项、恰好一个正确、三条 key_points |
| book_compare | 对齐后每行是否都有 relation + difference |

用法：`python scripts/smoke_modules.py`
任一环节失败退出码非 0，便于脚本化。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.domain.models  # noqa: F401,E402  触发全部模型注册
from app.core.config import settings  # noqa: E402
from app.domain.models.base import Base  # noqa: E402

DB_PATH = Path(tempfile.gettempdir()) / "cogcop_smoke_modules.db"
_ENGINE = None


def _fresh_session():
    global _ENGINE
    _ENGINE = create_engine(f"sqlite:///{DB_PATH}")
    Base.metadata.create_all(_ENGINE)
    return sessionmaker(bind=_ENGINE)()


def _hash_embedding():
    from app.retrieval.embedding import HashEmbedding

    return HashEmbedding()


# ---- 各链路 ----------------------------------------------------------------


def check_suggestion(session) -> tuple[bool, str]:
    from app.agent import intake, suggestion
    from app.ingestion.service import IngestionService
    from app.llm.gateway import ModelGateway

    item = IngestionService(session, _hash_embedding()).add_knowledge(
        user_id="smoke",
        title="原则 · 第三章",
        content=(
            "极度透明意味着组织里没有不能说的话。它讲的是信息在组织内部的可见性，"
            "不是决策权的分配。要让极度透明真正起作用，团队需要先建立说真话的安全感，"
            "否则它只会变成一句口号。"
        ),
    )
    ctx = intake.IntakeContext(
        question="怎么把极度透明用到周会上",
        goal="problem",
        self_level="once",
        help_type=["path"],
    )
    ctx.refresh_missing()

    outcome = suggestion.generate(
        session, user_id="smoke", ctx=ctx,
        gateway=ModelGateway(), embedding=_hash_embedding(),
    )
    if outcome.state == "insufficient_evidence":
        return False, f"依据不足（库里明明有相关内容）：{outcome.note}"
    if outcome.state != "ok":
        return False, f"state={outcome.state} note={outcome.note}"

    pool = {ev.evidence_id for ev in outcome.evidence}
    used = set()
    for row in outcome.items:
        used.update(row["basis_item_ids"])
        if not row["how_to_verify"]:
            return False, "建议缺 how_to_verify"
    if not used <= pool:
        return False, f"引用了不存在的依据：{used - pool}"
    # 判据是 `basis`（依据等级），不是 `source`（来源）。
    # 这里踩过一次：写成 source == "library" 永远不成立，把通过的链路报成了失败。
    from app.agent import grounding

    if not any(
        ev.basis == grounding.BASIS_LIBRARY
        for ev in outcome.evidence if ev.evidence_id in used
    ):
        return False, "没有一条 library 级依据"

    # 打印产出，人工看质量（自动校验只能保证结构对）
    preview = outcome.items[0]
    print(f"      建议：{preview['action'][:80]}…")
    print(f"      为什么：{preview['why'][:60]}")
    print(f"      怎么验：{preview['how_to_verify'][:60]}")
    return True, f"{len(outcome.items)} 条建议，{len(outcome.evidence)} 条证据，置信度 {outcome.confidence:.2f}"


def check_attribution(session) -> tuple[bool, str]:
    from app.agent import attribution
    from app.domain.repositories.claim_repository import ClaimRepository
    from app.domain.repositories.conflict_repository import ConflictRepository
    from app.domain.repositories.knowledge_repository import KnowledgeRepository
    from app.llm.gateway import ModelGateway

    krepo = KnowledgeRepository(session, user_id="smoke")
    crepo = ClaimRepository(session, user_id="smoke")
    item_a = krepo.create(
        user_id="smoke", title="数据库系统概念 · 索引",
        content=(
            "B+ 树索引把数据按顺序存放在叶子节点并用链表相连，因此非常适合范围查询与排序。"
            "对于一次要返回一个区间的查询，遍历叶子链表即可，代价与区间大小成正比。"
        ),
    )
    item_b = krepo.create(
        user_id="smoke", title="高性能 MySQL · 索引",
        content=(
            "哈希索引按键值算出桶号直接定位，等值查询是 O(1)。但它不支持范围查询，"
            "也无法用于排序，因此在只做单键点查的高并发场景里表现最好。"
        ),
    )
    claim_a = crepo.create(
        user_id="smoke", knowledge_item_id=item_a.id,
        statement="B+ 树更适合范围查询", topic="索引",
    )
    claim_b = crepo.create(
        user_id="smoke", knowledge_item_id=item_b.id,
        statement="哈希索引更适合等值查询", topic="索引",
    )
    conflict = ConflictRepository(session, user_id="smoke").create(
        user_id="smoke", item_a_id=item_a.id, item_b_id=item_b.id,
        claim_a_id=claim_a.id, claim_b_id=claim_b.id,
        conflict_type="适用边界", confidence=0.85,
    )
    session.commit()

    outcome = attribution.attribute_conflict(
        session, user_id="smoke", conflict_id=conflict.id, gateway=ModelGateway()
    )
    if outcome.state != "ok":
        return False, f"state={outcome.state} note={outcome.note}"

    a = outcome.attribution
    print(f"      原因类型：{a['cause_type']}")
    print(f"      背景A：{a['background_a'][:70]}")
    print(f"      背景B：{a['background_b'][:70]}")
    print(f"      场景：{outcome.scene_guidance[0]['scene'][:50]} → {outcome.scene_guidance[0]['follow']}")
    print(f"      判据：{outcome.probe['question'][:60]}")
    return True, f"{len(outcome.scene_guidance)} 条场景指引"


def check_probe(session) -> tuple[bool, str]:
    from app.agent import probe
    from app.ingestion.service import IngestionService
    from app.llm.gateway import ModelGateway

    IngestionService(session, _hash_embedding()).add_knowledge(
        user_id="smoke",
        title="原则 · 第三章",
        content=(
            "极度透明意味着组织里没有不能说的话。问题可以公开讨论，"
            "但对人的评价要区分事实与判断。极度透明的前提是安全感："
            "如果说出问题会被惩罚，透明就只剩形式。"
        ),
    )
    draft = probe.build_probe(session, user_id="smoke", gateway=ModelGateway())
    if draft is None:
        return False, "出题失败（模型输出不合格或调用异常，看上面的 WARNING）"

    correct = [o for o in draft.options if o.correct]
    print(f"      题目：{draft.question[:70]}")
    for option in draft.options:
        mark = "✓" if option.correct else " "
        print(f"      {mark} {option.key}. {option.text[:50]}")
    print(f"      要点：{draft.key_points[0][:50]}")
    return True, f"{len(draft.options)} 个选项（{len(correct)} 个正确），{len(draft.key_points)} 条要点"


def check_book_compare(session) -> tuple[bool, str]:
    from app.agent import book_compare
    from app.domain.models.book import Book
    from app.domain.models.book_agent_reading import BookAgentReading
    from app.llm.gateway import ModelGateway

    def _mk(title: str, chapters: list[dict]) -> Book:
        book = Book(
            user_id="smoke", title=title, author="", format="txt",
            file_path=f"{title}.txt", full_text="正文" * 100, total_chars=200,
            chapters=[
                {"index": i, "title": c["title"], "char_start": i * 100, "char_end": (i + 1) * 100}
                for i, c in enumerate(chapters, 1)
            ],
        )
        session.add(book)
        session.commit()
        session.add(BookAgentReading(
            user_id="smoke", book_id=book.id, status="done", total_chars=200,
            summary="总评", chapters_note=[
                {"index": i, "title": c["title"], "gist": c["gist"], "points": c["points"]}
                for i, c in enumerate(chapters, 1)
            ],
            chunk_count=3, failed_chunks=0, truncated=False,
        ))
        session.commit()
        return book

    book_a = _mk("数据库系统概念", [
        {"title": "索引与查询优化",
         "gist": "按访问模式选择索引结构，范围查询用 B+ 树，因为叶子节点有序且相连。",
         "points": ["范围查询遍历叶子链表", "排序可直接利用索引顺序"]},
        {"title": "事务与隔离级别",
         "gist": "四种隔离级别分别对应不同的并发异常：脏读、不可重复读、幻读。",
         "points": ["隔离级别越高并发越低", "可串行化能避免幻读"]},
    ])
    book_b = _mk("高性能 MySQL", [
        {"title": "索引与查询优化",
         "gist": "先看慢查询日志确认访问模式，再决定建哪种索引；哈希索引适合单键点查。",
         "points": ["慢查询日志是调优起点", "哈希索引等值查询 O(1)"]},
        {"title": "反范式的时机",
         "gist": "读多写少且连接成为瓶颈时，应主动反范式用冗余换查询次数。",
         "points": ["反范式有写入一致性代价", "只在读放大严重时采用"]},
    ])

    outcome = book_compare.compare(
        session, user_id="smoke", book_a_id=book_a.id, book_b_id=book_b.id,
        gateway=ModelGateway(),
    )
    if outcome.state != "ok":
        return False, f"state={outcome.state} note={outcome.note}"

    print(f"      总评：{outcome.overview[:80]}")
    for row in outcome.rows[:3]:
        print(f"      [{row['relation']}] {row['topic'][:24]} —— {row['difference'][:60]}")
    if not outcome.aligned:
        return False, "对齐结果为空却返回了 ok"
    return True, f"{len(outcome.rows)} 行对比，{len(outcome.aligned)} 组对齐"


# ---- 主流程 ----------------------------------------------------------------


def main() -> int:
    if not settings.deepseek_api_key:
        print("✗ 没有读到 DEEPSEEK_API_KEY，先检查 .env")
        return 2
    print(f"模型链：deepseek（key 已配置，长度 {len(settings.deepseek_api_key)}）")
    print(f"临时库：{DB_PATH.name}\n")

    if DB_PATH.exists():
        DB_PATH.unlink()
    session = _fresh_session()

    checks = [
        ("B 阅读建议（reading_suggestion）", check_suggestion),
        ("D 冲突归因（conflict_attribution）", check_attribution),
        ("E 出判断题（probe_question）", check_probe),
        ("C 两本书对比（book_compare）", check_book_compare),
    ]
    failed = 0
    for index, (name, fn) in enumerate(checks, 1):
        print(f"[{index}/{len(checks)}] {name}")
        started = time.monotonic()
        try:
            ok, detail = fn(session)
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本要打印而不是抛栈中断
            failed += 1
            print(f"    ✗ 异常：{type(exc).__name__}: {exc}")
            traceback.print_exc(limit=3)
            print()
            continue
        elapsed = time.monotonic() - started
        print(f"    {'✓' if ok else '✗'} {detail}（{elapsed:.1f}s）\n")
        if not ok:
            failed += 1

    session.close()
    # 必须先释放连接池再删文件：SQLite 文件被连接占着时，删除会以
    # "trash operation aborted" 这种完全不相干的说法失败（已踩过）。
    if _ENGINE is not None:
        _ENGINE.dispose()
    try:
        if DB_PATH.exists():
            DB_PATH.unlink()
    except OSError as exc:
        # 某些环境的安全删除钩子仍会拦截；临时目录里的文件留着不影响结论
        print(f"（临时库未能自动删除：{exc}，可手动删 {DB_PATH}）")

    print("=" * 60)
    if failed:
        print(f"✗ {failed} 条链路未通过")
        return 1
    print("✓ 四条链路全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
