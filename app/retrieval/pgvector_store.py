"""pgvector 向量库后端（ADR-08 起步选型）：向量下沉到 PostgreSQL + pgvector。

与 `InMemoryVectorStore` 同一 `VectorStore` 接口，差异只在「库内检索」落在数据库里：
- `search` 用 pgvector 的余弦距离算子（`<=>`）在库内排序取 top-k；
- `keyword_search` 用 `ILIKE` 子串召回 + 应用层 `score_keyword` 打分（与内存后端同款语义）；
- `upsert` / `delete` 直接写删专用表 `vector_store_table`。

惰性：`pgvector` / 数据库连接都在首次真正调用时才触碰——未安装 pgvector 时构造
不报错、首次调用给清晰提示；默认 SQLite 环境下不会拖慢启动，也不会影响其它功能。
"""
from __future__ import annotations

import logging

from app.core.config import settings
from app.retrieval.keyword import score_keyword
from app.retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)


class PgvectorStore(VectorStore):
    def __init__(
        self,
        session_factory=None,
        table_name: str | None = None,
        dim: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._table_name = table_name or settings.vector_store_table
        self._dim = int(dim or settings.embedding_dim)
        self._table = None

    # ---- 惰性依赖 ---------------------------------------------------

    def _deps(self):
        """校验 pgvector 依赖（惰性 import），缺失时给出可直接行动的提示。"""
        try:
            from pgvector.sqlalchemy import Vector  # noqa: F401
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "vector_backend=pgvector 需要 pgvector（pip install pgvector）"
                "以及可用的 PostgreSQL + psycopg 驱动"
            ) from exc
        return Vector

    def _session(self):
        if self._session_factory is None:
            from app.domain.db import SessionLocal

            self._session_factory = SessionLocal
        return self._session_factory()

    def _ensure_table(self):
        """建表 + 建扩展（幂等）。首次调用才连库。"""
        if self._table is not None:
            return self._table
        Vector = self._deps()
        from sqlalchemy import Column, MetaData, String, Table, Text

        metadata = MetaData()
        self._table = Table(
            self._table_name,
            metadata,
            Column("id", String(36), primary_key=True),
            Column("user_id", String(36), nullable=False, index=True),
            Column("title", String(256), default=""),
            Column("content", Text, default=""),
            # 可为空：向量化失败的条目仍写入（仅文本），供关键词兜底召回
            Column("embedding", Vector(self._dim), nullable=True),
        )
        session = self._session()
        try:
            engine = session.get_bind()
            from sqlalchemy import text

            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                metadata.create_all(conn)
        finally:
            session.close()
        return self._table

    @staticmethod
    def _vec_literal(vector: list[float]) -> str:
        return "[" + ",".join(f"{float(x):.8g}" for x in vector) + "]"

    # ---- VectorStore 接口 -------------------------------------------

    def upsert(
        self,
        *,
        item_id: str,
        user_id: str,
        vector: list[float] | None = None,
        title: str = "",
        content: str = "",
    ) -> None:
        self._ensure_table()
        session = self._session()
        try:
            from sqlalchemy import text

            session.execute(
                text(
                    f"""
                    INSERT INTO {self._table_name} (id, user_id, title, content, embedding)
                    VALUES (:id, :user_id, :title, :content, :vec)
                    ON CONFLICT (id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        embedding = EXCLUDED.embedding
                    """
                ),
                {
                    "id": item_id,
                    "user_id": user_id,
                    "title": title,
                    "content": content,
                    # vector 为 None 时存 NULL（仅文本，关键词兜底）；否则转 pgvector 字面量
                    "vec": self._vec_literal(vector) if vector is not None else None,
                },
            )
            session.commit()
        finally:
            session.close()

    def delete(self, *, item_id: str, user_id: str) -> None:
        self._ensure_table()
        session = self._session()
        try:
            from sqlalchemy import text

            session.execute(
                text(f"DELETE FROM {self._table_name} WHERE id = :id AND user_id = :user_id"),
                {"id": item_id, "user_id": user_id},
            )
            session.commit()
        finally:
            session.close()

    def search(
        self,
        query_vec: list[float],
        *,
        user_id: str | None = None,
        top_k: int = 5,
        min_score: float = 0.0,
    ) -> list[tuple[str, float]]:
        self._ensure_table()
        session = self._session()
        try:
            from sqlalchemy import text

            # 余弦距离 <=> ：距离 0 表完全同向；相似度 = 1 - 距离。
            # WHERE 里按距离 <= 1 - min_score 过滤，等价于相似度 >= min_score；
            # embedding IS NOT NULL 排除未向量化条目（只走关键词通道）。
            sql = f"""
                SELECT id, 1 - (embedding <=> :vec::vector) AS score
                FROM {self._table_name}
                WHERE embedding IS NOT NULL
                  AND (embedding <=> :vec::vector) <= (1 - :min_score)
            """
            params = {"vec": self._vec_literal(query_vec), "min_score": float(min_score)}
            if user_id is not None:
                sql += " AND user_id = :user_id"
                params["user_id"] = user_id
            sql += " ORDER BY embedding <=> :vec::vector LIMIT :top_k"
            params["top_k"] = int(top_k)

            rows = session.execute(text(sql), params).fetchall()
            return [(str(r.id), float(r.score)) for r in rows]
        finally:
            session.close()

    def keyword_search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        self._ensure_table()
        session = self._session()
        try:
            from sqlalchemy import text

            pattern = f"%{query.strip()}%"
            sql = (
                f"SELECT id, title, content FROM {self._table_name} "
                "WHERE (title ILIKE :p OR content ILIKE :p)"
            )
            params = {"p": pattern}
            if user_id is not None:
                sql += " AND user_id = :user_id"
                params["user_id"] = user_id

            rows = session.execute(text(sql), params).fetchall()
            scored = [(str(r.id), score_keyword(query, r.title, r.content)) for r in rows]
            scored = [(iid, s) for iid, s in scored if s > 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[: int(top_k)]
        finally:
            session.close()
