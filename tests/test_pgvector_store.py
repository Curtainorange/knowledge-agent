"""pgvector 后端测试：**不连库**也能验的那部分（SQL 契约）。

这一组测试的来历：`search()` 原来的 SQL 写的是 `:vec::vector`（PostgreSQL 强转语法），
而 SQLAlchemy 的 `text()` 用 `(?<![:\\w\\x5c]):(\\w+)(?!:)` 识别绑定参数——`(?!:)` 这条前瞻
把 `:vec::vector` 认成了参数 **`ve`**，`:vec::vector` 于是原样留在语句里发给数据库。

为什么之前没被发现：`text()` **构造时不报错**，只要不真连 PostgreSQL 就一切正常；
调用失败又会被 `Retriever._vector_scores` 的 try/except 兜住、降级成「只有关键词通道」——
表现是「检索质量悄悄变差」，不是报错。所以这里把「SQL 里的绑定参数必须全部被识别」
钉成断言：**语句识别到的参数名集合，必须与代码传进去的参数字典完全一致。**
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.retrieval.pgvector_store import (
    PgvectorStore,
    _escape_like,
    _prefilter_patterns,
)


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return self._rows


class FakeSession:
    """记录 execute 收到的语句与参数，不做任何数据库工作。"""

    def __init__(self, rows=()):
        self.calls: list[tuple[object, dict]] = []
        self.commits = 0
        self.closed = False
        self._rows = list(rows)

    def execute(self, stmt, params=None):
        self.calls.append((stmt, params or {}))
        return _Result(self._rows)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _store(session: FakeSession, *, table: str = "embeddings", dim: int = 4) -> PgvectorStore:
    """构造一个不触库的 store：建表被替换成 no-op（否则会去连 PostgreSQL）。"""
    store = PgvectorStore(session_factory=lambda: session, table_name=table, dim=dim)
    store._ensure_table = lambda: None  # type: ignore[method-assign]
    return store


def _bind_params_of(sql: str) -> set[str]:
    """问 SQLAlchemy：这条 SQL 里它究竟认出了哪些绑定参数。"""
    return set(text(sql)._bindparams.keys())


# ---- 绑定参数：这次踩过的坑，钉死在所有语句上 -------------------------------


def test_search_sql_bind_params_all_recognized():
    """`search` 的 SQL 必须让 SQLAlchemy 认出全部参数。

    反例（今天修掉的写法）：`:vec::vector` → 只认出 `ve`，`vec` 反倒不在语句里，
    参数名集合与语句对不上。凡是两者不一致，都是这类「看着像绑定参数、其实不是」的坑。
    """
    session = FakeSession()
    store = _store(session)
    sql, params = store._search_sql([0.1, 0.2, 0.3, 0.4], user_id="u", top_k=5, min_score=0.05)

    assert _bind_params_of(sql) == set(params), f"参数名与语句识别结果不一致: {sql}"
    assert "ve" not in _bind_params_of(sql)  # 就是它把 vec 顶掉了
    # 向量参数必须走显式 CAST，而不是冒号强转
    assert "::vector" not in sql
    assert "CAST(:vec AS vector)" in sql


def test_upsert_and_delete_sql_bind_params_all_recognized():
    session = FakeSession()
    store = _store(session)

    store.upsert(item_id="i", user_id="u", vector=[0.1, 0.2, 0.3, 0.4], title="t", content="c")
    store.delete(item_id="i", user_id="u")

    assert len(session.calls) == 2
    for stmt, params in session.calls:
        sql = str(stmt)
        assert _bind_params_of(sql) == set(params), f"参数名与语句识别结果不一致: {sql}"
        assert "::vector" not in sql
    assert session.commits == 2


def test_search_executed_sql_matches_params():
    """走完整 `search()` 路径再验一遍：防止以后有人改调用点、绕过 `_search_sql`。"""
    session = FakeSession(rows=[])
    store = _store(session)
    store.search([0.5, 0.5, 0.0, 0.0], user_id="u", top_k=3, min_score=0.1)

    sql, params = session.calls[0][0], session.calls[0][1]
    assert _bind_params_of(str(sql)) == set(params)
    assert params["top_k"] == 3
    assert params["user_id"] == "u"
    assert params["min_score"] == 0.1


def test_search_omits_user_filter_when_user_id_is_none():
    session = FakeSession()
    store = _store(session)
    sql, params = store._search_sql([0.1, 0.2, 0.3, 0.4], user_id=None, top_k=5, min_score=0.0)

    assert "user_id" not in params
    assert "user_id =" not in sql


def test_search_returns_rows_as_id_score_pairs():
    class _Row:
        def __init__(self, id, score):
            self.id = id
            self.score = score

    session = FakeSession(rows=[_Row("a", 0.91), _Row("b", 0.42)])
    store = _store(session)
    hits = store.search([0.1, 0.2, 0.3, 0.4], user_id="u")
    assert hits == [("a", 0.91), ("b", 0.42)]


def test_upsert_empty_vector_is_stored_as_null():
    """空列表不等于合法向量：`'[]'` 会被数据库拒收，应存 NULL 走关键词通道。"""
    session = FakeSession()
    store = _store(session)
    store.upsert(item_id="i", user_id="u", vector=[], title="t", content="c")
    assert session.calls[0][1]["vec"] is None


# ---- 关键词通道：预筛语义必须与内存后端对齐 ---------------------------------


def test_prefilter_patterns_cover_multi_term_query():
    """预筛词要覆盖「词 + 词的二字窗口」。

    整串 ILIKE 只有 query 原样出现才命中——「B+树 范围查询」几乎必然漏召，
    关键词兜底在 pgvector 后端就成了一句空话（同一份语义在内存后端是好的）。
    """
    patterns = _prefilter_patterns("B+树 范围查询")
    assert "B+树" in patterns          # 整词
    assert "范围" in patterns          # 二字窗口
    assert "查询" in patterns
    assert "B+树 范围查询" not in patterns  # 不再要求整串原样出现


def test_prefilter_patterns_edge_cases():
    assert _prefilter_patterns("") == []
    assert _prefilter_patterns("   ") == []
    assert _prefilter_patterns("索引") == ["索引"]            # 单词：只按词匹配
    assert _prefilter_patterns("a") == ["a"]                 # 单字：没有二字窗口
    # 重复片段去重（同一片段写两遍会让 OR 白写）
    assert _prefilter_patterns("查询 查询") == ["查询"]


def test_keyword_sql_ors_each_pattern_and_scopes_user():
    session = FakeSession()
    store = _store(session)
    sql, params = store._keyword_sql("数据库 索引", user_id="u")

    assert _bind_params_of(sql) == set(params)
    assert " OR " in sql
    assert params["user_id"] == "u"
    # 每个预筛词各有一个参数，且都带上了两侧通配
    values = [v for k, v in params.items() if k.startswith("p")]
    assert all(v.startswith("%") and v.endswith("%") for v in values)
    assert any("数据库" in v for v in values)


def test_keyword_sql_escapes_like_wildcards():
    """查询里的 `%` 是普通字符，不该被当成通配符（否则「增长50%」会召回一堆无关条目）。"""
    assert _escape_like("50%") == "50\\%"
    assert _escape_like("a_b") == "a\\_b"

    session = FakeSession()
    store = _store(session)
    _, params = store._keyword_sql("50%", user_id="u")
    assert params["p0"] == "%50\\%%"
    assert "ESCAPE" in store._keyword_sql("50%", user_id="u")[0]


def test_keyword_search_scores_and_filters():
    """SQL 只做候选预筛，得分与过滤仍由 `score_keyword` 决定（与内存后端同函数）。"""

    class _Row:
        def __init__(self, id, title, content):
            self.id, self.title, self.content = id, title, content

    session = FakeSession(
        rows=[
            _Row("a", "数据库索引", "B+树与哈希索引的区别"),
            _Row("b", "完全无关", "xxxxxxxx"),
        ]
    )
    store = _store(session)
    hits = store.keyword_search("数据库 索引", user_id="u", top_k=5)

    assert [h[0] for h in hits] == ["a"]  # b 得分为 0，被滤掉
    assert hits[0][1] > 0


def test_keyword_search_respects_top_k():
    class _Row:
        def __init__(self, id, title):
            self.id, self.title, self.content = id, title, "索引索引索引"

    session = FakeSession(rows=[_Row("a", "索引甲"), _Row("b", "索引乙"), _Row("c", "索引丙")])
    store = _store(session)
    assert len(store.keyword_search("索引", user_id="u", top_k=2)) == 2


def test_keyword_search_empty_query_skips_database():
    session = FakeSession()
    store = _store(session)
    assert store.keyword_search("   ", user_id="u") == []
    assert session.calls == []  # 空查询不该白跑一次全表 OR 扫描


# ---- 表名：要拼进 SQL 的配置项，形状不对就早报错 ---------------------------


def test_table_name_is_validated():
    """表名是拼进 SQL 的标识符（不能走绑定参数），非法形状必须在构造时就拒绝。"""
    with pytest.raises(ValueError, match="vector_store_table"):
        PgvectorStore(table_name="embeddings; DROP TABLE users")
    with pytest.raises(ValueError, match="vector_store_table"):
        PgvectorStore(table_name="1bad")
    assert PgvectorStore(table_name="vector_store_2026")._table_name == "vector_store_2026"
