"""给「还没算向量」的知识条目补算 embedding。

什么时候会用到：
- 条目落库时向量计算失败（`embed_status=embed_failed`，例如模型临时不可用）；
- 或者某条链路漏算了向量（历史上微信读书同步漏传过 embedding，条目停在
  `embed_status=pending`）——这类条目**不会出现在语义检索结果里**，表现为
  「明明录进去了却搜不到」。

用法（在项目根目录执行）：
    python scripts/reembed_pending.py            # 补算全部未向量化的条目
    python scripts/reembed_pending.py --dry-run  # 只统计不写库

只处理未删除的条目；单条失败只记该条 `embed_failed`，不中断整批。
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.domain.db import SessionLocal
from app.domain.models.knowledge_item import KnowledgeItem
from app.retrieval.embedding import EmbeddingModel, build_embedding


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    dry_run = "--dry-run" in args

    session = SessionLocal()
    try:
        rows = list(
            session.scalars(
                select(KnowledgeItem).where(
                    KnowledgeItem.embed_status != "embedded",
                    KnowledgeItem.is_deleted.is_(False),
                )
            )
        )
        counts: dict[str, int] = {}
        for item in rows:
            counts[item.embed_status] = counts.get(item.embed_status, 0) + 1
        print(f"待补算条目：{len(rows)} 条 {counts}")
        if dry_run or not rows:
            print("（--dry-run，未写库）" if dry_run else "（无待补算条目）")
            return 0

        embedding = build_embedding()
        done = failed = 0
        for item in rows:
            try:
                vector = embedding.embed([item.title + "\n" + item.raw_content])[0]
                item.embedding = EmbeddingModel.dumps(vector)
                item.embed_status = "embedded"
                done += 1
            except Exception as exc:  # noqa: BLE001 - 单条失败不拖垮整批
                item.embed_status = "embed_failed"
                failed += 1
                print(f"  [FAIL] {item.id}: {exc}")
        session.commit()
        print(f"完成：成功 {done} 条，失败 {failed} 条")
        return 0 if not failed else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
