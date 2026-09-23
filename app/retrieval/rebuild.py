"""嵌入向量增量重建（借鉴 agentic-local-brain 的供应商耦合教训）。

问题背景：换嵌入模型后，旧向量与新查询向量不可比（维度不同 / 语义空间不同），
检索会静默劣化。没有版本记录时只能全库盲刷；有了 `embedding_model` /
`embedding_dim` 记录，就能只重算「模型不一致或缺失」的条目。

入口：
- `rebuild_stale_embeddings(session, ...)`：服务函数（tests / 脚本共用）；
- `scripts/rebuild_embeddings.py`：命令行包装。

失败语义与录入路径一致（可靠-4）：单条重算失败只计数、保留原状，不阻断其余条目。
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models.knowledge_item import KnowledgeItem
from app.retrieval.embedding import EmbeddingModel, build_embedding
from app.retrieval.vector_store import VectorStore, build_vector_store

logger = logging.getLogger(__name__)


def find_stale_items(session: Session, embedding_name: str) -> list[KnowledgeItem]:
    """需要重建的条目：未删除，且模型标识与当前不一致或从未记录。"""
    rows = session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.is_deleted.is_(False))
    ).all()
    return [
        row
        for row in rows
        if row.embedding_model != embedding_name or row.embedding is None
    ]


def rebuild_stale_embeddings(
    session: Session,
    *,
    embedding: EmbeddingModel | None = None,
    store: VectorStore | None = None,
    limit: int | None = None,
    batch_size: int = 20,
) -> dict:
    """重算陈旧条目的向量并同步索引，返回统计。

    分批提交：模型加载 + 批量推理可能耗时较长，长事务会占住 SQLite 写锁
    （与录入路径同一教训）。`limit` 供分次执行 / 灰度验证用。
    """
    embedding = embedding or build_embedding()
    store = store or build_vector_store()

    all_rows = session.scalars(
        select(KnowledgeItem).where(KnowledgeItem.is_deleted.is_(False))
    ).all()
    stale = [r for r in all_rows if r.embedding_model != embedding.name or r.embedding is None]
    if limit is not None:
        stale = stale[:limit]

    rebuilt = failed = 0
    for index, item in enumerate(stale, start=1):
        try:
            vec = embedding.embed([item.title + "\n" + item.raw_content])[0]
            item.embedding = EmbeddingModel.dumps(vec)
            item.embed_status = "embedded"
            item.embedding_model = embedding.name
            item.embedding_dim = len(vec)
            store.upsert(
                item_id=item.id,
                user_id=item.user_id,
                vector=vec,
                title=item.title,
                content=item.raw_content,
            )
            rebuilt += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断其余条目（可靠-4）
            failed += 1
            logger.warning("rebuild embed failed item=%s: %s", item.id, exc)
        if index % batch_size == 0:
            session.commit()
    session.commit()

    return {
        "total": len(all_rows),
        "stale": len(stale),
        "rebuilt": rebuilt,
        "failed": failed,
        "model": embedding.name,
        "dim": getattr(embedding, "dim", None),
    }
