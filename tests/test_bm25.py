"""BM25 稀疏检索测试：分词、IDF、tf 饱和、长度归一化，以及内存后端的接线。

这些用例的存在理由：`tests/data/retrieval_eval.json` 那套评估集测不出 overlap 与 BM25
的差别（16 条查询词面高度重叠，两种打分 recall@1 都是 0.812），而**换实现的理由恰恰
在评估集覆盖不到的地方**——长笔记擦边一次关键词、高频词压过罕见词。所以这里用构造语料
做定向断言，`test_overlap_*` 那两条是「为什么换」的对照，改回去就该失败。
"""
from __future__ import annotations

import pytest

from app.retrieval.bm25 import BM25Index, tokenize
from app.retrieval.embedding import HashEmbedding
from app.retrieval.vector_store import (
    LEXICAL_BM25,
    LEXICAL_OVERLAP,
    InMemoryVectorStore,
    VectorRecord,
)

# ---- 分词 -------------------------------------------------------------------


def test_tokenize_chinese_uses_bigram_windows():
    assert tokenize("数据库") == ["数据", "据库"]


def test_tokenize_does_not_cross_whitespace():
    """按段展开：跨空格会生出「引范」这种噪音 token。"""
    assert "引范" not in tokenize("数据库索引 范围查询")
    assert tokenize("数据库索引 范围查询") == ["数据", "据库", "库索", "索引", "范围", "围查", "查询"]


def test_tokenize_keeps_single_char_and_ascii_words():
    assert tokenize("树") == ["树"]
    assert tokenize("git rebase 改写提交") == ["git", "rebase", "改写", "写提", "提交"]


def test_tokenize_handles_mixed_script():
    assert tokenize("B+树") == ["b", "树"]


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


# ---- 索引维护 ---------------------------------------------------------------


def test_add_is_overwrite_semantics():
    """与 VectorStore.upsert 对齐：同一 doc_id 先删再加，旧 token 不能残留。"""
    idx = BM25Index()
    idx.add("a", "旧标题", "旧的内容在这里")
    idx.add("a", "新标题", "新的内容在这里")
    assert len(idx) == 1
    assert idx.score("旧的") == {}
    assert idx.score("新的")


def test_remove_is_idempotent():
    idx = BM25Index()
    idx.add("a", "标题", "内容")
    idx.remove("a")
    idx.remove("a")
    assert len(idx) == 0
    assert idx.score("内容") == {}


def test_empty_doc_is_not_indexed():
    idx = BM25Index()
    idx.add("a", "", "")
    assert len(idx) == 0


# ---- 三个机制（换掉重叠率的全部理由） ----------------------------------------


def _docs_of(lexical: str, docs: list[tuple[str, str, str]]) -> InMemoryVectorStore:
    store = InMemoryVectorStore(lexical_backend=lexical)
    emb = HashEmbedding()
    for item_id, title, content in docs:
        store.upsert(
            item_id=item_id,
            user_id="u",
            vector=emb.embed([content])[0],
            title=title,
            content=content,
        )
    return store


def _idf_corpus() -> list[tuple[str, str, str]]:
    """8 条反复讲「数据库」，只有 1 条提到「索引」。"""
    docs = [(f"common{i}", "数据库基础", "数据库的概念与常识") for i in range(8)]
    docs.append(("rare", "笔记", "这里讲索引"))
    return docs


def test_bm25_idf_puts_rare_term_first():
    store = _docs_of(LEXICAL_BM25, _idf_corpus())
    hits = store.keyword_search("数据库 索引", user_id="u", top_k=1)
    assert hits[0][0] == "rare"


def test_overlap_loses_rare_term_to_frequent_one():
    """对照：重叠率不看词有多常见，罕见词命中的条目被 8 条高频词文档挤出 top3。

    改回 `lexical_backend=overlap` 时这条会失败——这就是不该退回去的证据。
    """
    store = _docs_of(LEXICAL_OVERLAP, _idf_corpus())
    hits = [item_id for item_id, _ in store.keyword_search("数据库 索引", user_id="u", top_k=3)]
    assert "rare" not in hits


def test_bm25_length_normalization_prefers_short_doc():
    """同样只提一次关键词，短卡片应压过被无关内容稀释的长笔记。"""
    long_content = "读书随想，与主题无关。" * 30 + "这里提一次索引。"
    store = _docs_of(
        LEXICAL_BM25,
        [("short_doc", "笔记甲", "这里讲索引"), ("long_doc", "笔记乙", long_content)],
    )
    scores = dict(store.keyword_search("索引", user_id="u", top_k=5))
    assert scores["short_doc"] > scores["long_doc"]


def test_overlap_cannot_separate_short_from_diluted_long():
    """对照：重叠率与文档长度无关，两条并列同分，排序只能靠插入顺序。"""
    long_content = "读书随想，与主题无关。" * 30 + "这里提一次索引。"
    store = _docs_of(
        LEXICAL_OVERLAP,
        [("short_doc", "笔记甲", "这里讲索引"), ("long_doc", "笔记乙", long_content)],
    )
    scores = dict(store.keyword_search("索引", user_id="u", top_k=5))
    assert scores["short_doc"] == pytest.approx(scores["long_doc"])


def _score_single(content: str) -> float:
    idx = BM25Index()
    idx.add("a", "笔记", content)
    return idx.score("索引")["a"]


def test_bm25_tf_saturates():
    """出现 10 次的得分应远小于出现 2 次的 5 倍（k1 饱和项在起作用）。"""
    two = _score_single("索引索引 其它内容")
    ten = _score_single("索引" * 10 + " 其它内容")
    assert ten > two
    assert ten < 5 * two


# ---- 内存后端接线 -----------------------------------------------------------


def test_store_default_lexical_backend_is_bm25():
    assert InMemoryVectorStore().lexical_backend == LEXICAL_BM25


def test_store_rejects_unknown_lexical_backend():
    with pytest.raises(ValueError, match="lexical_backend"):
        InMemoryVectorStore(lexical_backend="bm42")


def test_store_delete_removes_from_bm25_index():
    """软删后不能继续被关键词通道召回。"""
    store = _docs_of(LEXICAL_BM25, [("a", "索引", "B+树索引")])
    assert store.keyword_search("索引", user_id="u")
    store.delete(item_id="a", user_id="u")
    assert store.keyword_search("索引", user_id="u") == []


def test_backfill_indexes_bm25_too():
    """回填只补 _records 不补 BM25，重启后关键词通道会直接召空。"""
    store = InMemoryVectorStore(
        loader=lambda: [
            VectorRecord(item_id="a", user_id="u", title="索引", content="B+树索引", vector=[1.0, 0.0])
        ],
        lexical_backend=LEXICAL_BM25,
    )
    assert [item_id for item_id, _ in store.keyword_search("索引", user_id="u")] == ["a"]


def test_bm25_respects_user_scope():
    """倒排表是全局的，跨用户不可见必须在打分之后按归属过滤。"""
    store = InMemoryVectorStore(lexical_backend=LEXICAL_BM25)
    store.upsert(item_id="a", user_id="uA", vector=None, title="索引", content="B+树索引")
    assert store.keyword_search("索引", user_id="uB") == []
    assert [item_id for item_id, _ in store.keyword_search("索引", user_id="uA")] == ["a"]


def test_bm25_ignores_unindexed_doc_left_in_postings():
    """索引与记录不同步时宁可漏召回，也不返回幽灵条目。"""
    store = InMemoryVectorStore(lexical_backend=LEXICAL_BM25)
    store.upsert(item_id="a", user_id="u", vector=None, title="索引", content="B+树索引")
    store._records.clear()  # 模拟残留：bm25 里还有倒排，记录已不在
    assert store.keyword_search("索引", user_id="u") == []
