"""重建陈旧嵌入向量：换嵌入模型后跑一次，只重算模型不一致或缺失的条目。

用法::

    python scripts/rebuild_embeddings.py            # 全量重建陈旧条目
    python scripts/rebuild_embeddings.py --limit 50 # 只重建 50 条（分次执行）
    python scripts/rebuild_embeddings.py --dry-run  # 只统计，不动数据

**先跑迁移再跑本脚本**：依赖 0012 迁移加的 embedding_model / embedding_dim 列
（alembic upgrade head）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.db import SessionLocal  # noqa: E402
from app.retrieval.embedding import build_embedding  # noqa: E402
from app.retrieval.rebuild import find_stale_items, rebuild_stale_embeddings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重建陈旧嵌入向量（模型不一致 / 缺失的条目）")
    parser.add_argument("--limit", type=int, default=None, help="本次最多重建多少条（默认不限）")
    parser.add_argument("--dry-run", action="store_true", help="只统计需要重建的数量，不执行")
    args = parser.parse_args()

    embedding = build_embedding()
    session = SessionLocal()
    try:
        stale = find_stale_items(session, embedding.name)
        print(f"当前嵌入模型：{embedding.name}（dim={getattr(embedding, 'dim', '?')}）")
        print(f"需要重建：{len(stale)} 条")
        if args.dry_run or not stale:
            return 0
        stats = rebuild_stale_embeddings(session, embedding=embedding, limit=args.limit)
        print(
            f"重建完成：共 {stats['total']} 条，重算 {stats['rebuilt']} 条，"
            f"失败 {stats['failed']} 条"
        )
        if stats["failed"]:
            print("失败条目保留原状，可重跑本脚本重试（详见日志）", file=sys.stderr)
            return 1
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
