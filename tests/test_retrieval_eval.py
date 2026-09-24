"""检索质量评估集（借鉴 agentic-local-brain 的评估思路，落地为本项目护栏）。

- 语料与查询在 tests/data/retrieval_eval.json（20 条语料 / 16 条查询）。
- 指标：recall@k = 命中至少一条相关条目的查询占比（hit-rate）。
- 全程用 HashEmbedding + 内存索引，确定性、不触网；哈希向量语义弱但确定，
  作为**回归基线**足够：融合策略改动后指标不应低于护栏值，也不应低于旧策略。
- 换真语义模型后此文件不变——指标只会更好，护栏依然有效。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.retrieval.embedding import HashEmbedding
from app.retrieval.retriever import FUSION_RRF, FUSION_WEIGHTED, Retriever
from app.retrieval.vector_store import InMemoryVectorStore

EVAL_PATH = Path(__file__).resolve().parent / "data" / "retrieval_eval.json"
TOP_K = 5
# 护栏值（低于它即回归）。实测（哈希向量 + 本评估集，2026-09-23）：
# recall@5 = 1.00、recall@1 = 0.81，两种融合持平。护栏取实测减余量，
# 只防大幅回退，不为微小抖动报警。
RECALL5_FLOOR = 0.95
RECALL1_FLOOR = 0.75


def _load_eval() -> tuple[list[dict], list[dict]]:
    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    return data["corpus"], data["queries"]


def _build_retriever(fusion: str) -> Retriever:
    corpus, _ = _load_eval()
    store = InMemoryVectorStore()
    emb = HashEmbedding()
    for doc in corpus:
        vec = emb.embed([doc["title"] + "\n" + doc["content"]])[0]
        store.upsert(item_id=doc["id"], user_id="u", vector=vec, title=doc["title"], content=doc["content"])
    return Retriever(emb, store, fusion=fusion)


def _recall(retriever: Retriever, queries: list[dict], k: int) -> float:
    hits = 0
    for case in queries:
        top = [h.item_id for h in retriever.retrieve(case["q"], user_id="u", top_k=k)]
        if set(case["relevant"]) & set(top):
            hits += 1
    return hits / len(queries)


@pytest.fixture(scope="module")
def eval_queries() -> list[dict]:
    _, queries = _load_eval()
    return queries


@pytest.mark.parametrize("fusion", [FUSION_RRF, FUSION_WEIGHTED])
def test_recall5_above_floor(fusion, eval_queries):
    assert _recall(_build_retriever(fusion), eval_queries, TOP_K) >= RECALL5_FLOOR


@pytest.mark.parametrize("fusion", [FUSION_RRF, FUSION_WEIGHTED])
def test_recall1_above_floor(fusion, eval_queries):
    assert _recall(_build_retriever(fusion), eval_queries, 1) >= RECALL1_FLOOR


def test_rrf_is_no_worse_than_weighted_on_eval_set(eval_queries):
    """RRF 换掉加权融合的门槛：在评估集上不能更差（两者确定性，结果可精确比较）。"""
    weighted = _recall(_build_retriever(FUSION_WEIGHTED), eval_queries, TOP_K)
    rrf = _recall(_build_retriever(FUSION_RRF), eval_queries, TOP_K)
    assert rrf >= weighted
