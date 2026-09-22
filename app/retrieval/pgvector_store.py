"""pgvector 向量库后端（ADR-08 起步选型）：向量下沉到 PostgreSQL + pgvector。

与 `InMemoryVectorStore` 同一 `VectorStore` 接口，差异只在「库内检索」落在数据库里：
- `search` 用 pgvector 的余弦距离算子（`<=>`）在库内排序取 top-k；
- `keyword_search` 用 `ILIKE` 子串**预筛** + 应用层 `score_keyword` 打分（与内存后端同款语义）；
- `upsert` / `delete` 直接写删专用表 `vector_store_table`。

惰性：`pgvector` / 数据库连接都在首次真正调用时才触碰——未安装 pgvector 时构造
不报错、首次调用给清晰提示；默认 SQLite 环境下不会拖慢启动，也不会影响其它功能。

两处**只能真机才能验证**、代码里无法自证的地方，切到 PostgreSQL 时先确认：
1. **没有建 ANN 索引**（ivfflat / hnsw），`<=>` 走的是全表顺序扫描。单用户数百条无感，
   上万条就该补索引了——建索引要在数据写入**之后**（ivfflat 需要先有数据才能聚类）。
2. 参数都以**字符串字面量**传（`CAST(:vec AS vector)`），依赖 pgvector 的输入函数解析
   `"[0.1,0.2]"`。已按 psycopg 两种驱动的取值范围写，但只有真连上才算验过。

**SQL 里写向量参数务必用 `CAST(:vec AS vector)`，不要写 `:vec::vector`。**
SQLAlchemy 的 `text()` 用正则识别绑定参数，`(?!:)` 这条前瞻会把 `:vec::vector` 解析成
参数 **`ve`**，`:vec::vector` 原样留在语句里发给数据库 → 必然报错。这个坑很隐蔽：
`text()` 构造不报错、单元测试只要不连库也发现不了（本项目就踩过一次，见
`tests/test_pgvector_store.py` 的绑定参数回归测试）。
"""
from __future__ import annotations

import logging
import re

from app.core.config import settings
from app.retrieval.keyword import score_keyword
from app.retrieval.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 表名是配置项、要拼进 SQL（标识符不能走绑定参数），所以只允许安全标识符
_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _escape_like(text: str) -> str:
    """转义 LIKE 通配符。查询里带 `%`（如「增长了50%」）时不该被当成通配符。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _prefilter_patterns(query: str) -> list[str]:
    """关键词通道的 SQL 预筛词。

    **不能拿整条 query 去 ILIKE**：`score_keyword` 是按二字窗口（n-gram）重叠打分的，
    而整串子串匹配要求 query 一字不差地出现在标题或正文里——像「B+树 范围查询」
    这样的多词查询会一条都召不回，关键词兜底就等于没有（可靠-4 只在内存后端成立，
    切到 pgvector 会静默失效）。所以这里按「词 + 词的二字窗口」展开成 OR 条件，
    与 `score_keyword` 的语义对齐：命中任一片段即进入候选，再由打分函数排序。
    """
    stripped = (query or "").strip()
    if not stripped:
        return []
    tokens = [t for t in stripped.split() if t] or [stripped]
    patterns: list[str] = []
    for token in tokens:
        patterns.append(token)
        patterns.extend(token[i : i + 2] for i in range(len(token) - 1))
    # 去重且保序：同一片段出现两次会让 OR 条件白写一遍
    return list(dict.fromkeys(patterns))


class PgvectorStore(VectorStore):
    def __init__(
        self,
        session_factory=None,
        table_name: str | None = None,
        dim: int | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._table_name = table_name or settings.vector_store_table
        if not _TABLE_NAME_RE.match(self._table_name):
            # 表名要拼进 SQL（标识符不能绑定参数），形状不对就直接拒绝，别留到执行时才报错
            raise ValueError(f"非法的 vector_store_table: {self._table_name!r}")
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
        # 空列表等于「没有向量」：``'[]'`` 不是合法 vector 字面量，会被数据库拒收
        literal = self._vec_literal(vector) if vector else None
        session = self._session()
        try:
            from sqlalchemy import text

            session.execute(
                text(
                    f"""
                    INSERT INTO {self._table_name} (id, user_id, title, content, embedding)
                    VALUES (:id, :user_id, :title, :content, CAST(:vec AS vector))
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
                    "vec": literal,
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

    def _search_sql(
        self,
        query_vec: list[float],
        *,
        user_id: str | None,
        top_k: int,
        min_score: float,
    ) -> tuple[str, dict]:
        """拼检索 SQL 与参数（独立成方法，好让测试直接检查绑定参数）。

        余弦距离 `<=>`：距离 0 表完全同向，相似度 = 1 - 距离；
        WHERE 里按 `距离 <= 1 - min_score` 过滤，等价于 `相似度 >= min_score`。
        `embedding IS NOT NULL` 排除未向量化条目（它们只走关键词通道）。
        """
        sql = f"""
            SELECT id, 1 - (embedding <=> CAST(:vec AS vector)) AS score
            FROM {self._table_name}
            WHERE embedding IS NOT NULL
              AND (embedding <=> CAST(:vec AS vector)) <= (1 - :min_score)
        """
        params: dict = {
            "vec": self._vec_literal(query_vec),
            "min_score": float(min_score),
        }
        if user_id is not None:
            sql += " AND user_id = :user_id"
            params["user_id"] = user_id
        sql += " ORDER BY embedding <=> CAST(:vec AS vector) LIMIT :top_k"
        params["top_k"] = int(top_k)
        return sql, params

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

            sql, params = self._search_sql(
                query_vec, user_id=user_id, top_k=top_k, min_score=min_score
            )
            rows = session.execute(text(sql), params).fetchall()
            return [(str(r.id), float(r.score)) for r in rows]
        finally:
            session.close()

    def _keyword_sql(self, query: str, *, user_id: str | None) -> tuple[str, dict]:
        """关键词预筛 SQL（独立成方法，同样是为了可测）。"""
        patterns = _prefilter_patterns(query)
        if not patterns:
            return "", {}
        clauses = []
        params: dict = {}
        for idx, pattern in enumerate(patterns):
            key = f"p{idx}"
            clauses.append(f"(title ILIKE :{key} ESCAPE '\\' OR content ILIKE :{key} ESCAPE '\\')")
            params[key] = f"%{_escape_like(pattern)}%"
        sql = (
            f"SELECT id, title, content FROM {self._table_name} WHERE "
            + " OR ".join(clauses)
        )
        if user_id is not None:
            sql += " AND user_id = :user_id"
            params["user_id"] = user_id
        return sql, params

    def keyword_search(
        self,
        query: str,
        *,
        user_id: str | None = None,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        sql, params = self._keyword_sql(query, user_id=user_id)
        if not sql:
            return []
        self._ensure_table()
        session = self._session()
        try:
            from sqlalchemy import text

            rows = session.execute(text(sql), params).fetchall()
            # SQL 只做「候选预筛」，最终得分仍由与内存后端同一个函数给出
            scored = [(str(r.id), score_keyword(query, r.title, r.content)) for r in rows]
            scored = [(iid, s) for iid, s in scored if s > 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[: int(top_k)]
        finally:
            session.close()
