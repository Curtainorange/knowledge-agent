"""L2 冲突检测测试：增量提取 / 候选对 / 判定 / 幂等 / 阈值 / 反馈抑制 / API。

按测试铁律全程不触真实模型：FakeProvider 按 task_type 回放预设 JSON。
"""
from __future__ import annotations

import json

import pytest

from app.agent.l2_orchestrator import L2Orchestrator
from app.api import deps
from app.domain.models.claim import Claim
from app.domain.models.conflict import Conflict
from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import ConflictRepository, make_pair_key
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from app.retrieval.embedding import EmbeddingModel
from tests.helpers import auth_headers


class FakeProvider(LLMProvider):
    """按 task_type 回放预设响应；队列为空时给出安全默认值。

    预案值放字符串时直接作为原始文本返回（用于构造解析失败场景）。
    """

    def __init__(self, rows_by_task: dict[str, list] | None = None) -> None:
        self.rows_by_task = {k: list(v) for k, v in (rows_by_task or {}).items()}
        self.calls: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(task_type)
        rows = self.rows_by_task.get(task_type)
        if rows:
            row = rows.pop(0)
            text = row if isinstance(row, str) else json.dumps(row)
        elif task_type == "batch_extraction":
            text = json.dumps({"claims": []})
        else:
            text = json.dumps({"relation": "无关", "confidence": 0.9})
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


def _orchestrator(session, rows_by_task) -> L2Orchestrator:
    return L2Orchestrator(ModelGateway(provider=FakeProvider(rows_by_task)), session)


def _ingest(session, user_id: str, title: str, content: str):
    repo = KnowledgeRepository(session, user_id=user_id)
    item = repo.create(user_id=user_id, title=title, content=content)
    session.commit()
    return item


# ---------- L2-1 增量主张提取 ----------


def test_scan_extracts_claims_and_marks_scanned(session):
    item = _ingest(session, "u1", "长期主义者的决策框架", "坚守既定战略至少三年。")
    orch = _orchestrator(session, {
        "batch_extraction": [{
            "claims": [
                {"statement": "应坚守既定战略至少三年", "topic": "决策策略", "polarity": 1, "strength": 0.9},
                {"statement": "频繁调整方向会损耗组织", "topic": "组织", "polarity": 1, "strength": 0.6},
            ]
        }],
    })
    result = orch.scan(user_id="u1")

    assert result.scanned_items == 1
    assert result.claims_extracted == 2
    assert result.extraction_failures == 0
    claims = ClaimRepository(session, user_id="u1").list_by_item(item.id)
    assert len(claims) == 2
    session.refresh(item)
    assert item.claims_scanned_at is not None


def test_scan_persists_claim_embedding(session):
    """主张提取成功后把向量写入 Claim.embedding（供后续向量近邻复用）。"""
    item = _ingest(session, "u1", "长期主义", "坚守战略。")
    orch = _orchestrator(session, {
        "batch_extraction": [{
            "claims": [
                {"statement": "应坚守既定战略至少三年", "topic": "决策策略", "polarity": 1, "strength": 0.9},
            ]
        }],
    })
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_item(item.id)
    assert len(claims) == 1
    assert claims[0].embedding is not None  # 主张向量已落库


def test_scan_is_incremental(session):
    """已扫描且未更新的条目不会重复提取。"""
    _ingest(session, "u1", "长期主义", "坚守战略。")
    provider = FakeProvider({"batch_extraction": [{"claims": [
        {"statement": "坚守战略至少三年", "topic": "决策策略", "polarity": 1, "strength": 0.9},
    ]}]})
    orch = L2Orchestrator(ModelGateway(provider=provider), session)

    first = orch.scan(user_id="u1")
    assert first.claims_extracted == 1
    second = orch.scan(user_id="u1")

    assert second.scanned_items == 0
    assert provider.calls.count("batch_extraction") == 1


def test_rescan_after_item_update(session):
    """条目更新后自动重扫，旧主张被替换而不是叠加。"""
    item = _ingest(session, "u1", "长期主义", "坚守战略。")
    provider = FakeProvider({"batch_extraction": [
        {"claims": [{"statement": "旧主张", "topic": "决策策略", "polarity": 1, "strength": 0.9}]},
        {"claims": [{"statement": "新主张", "topic": "决策策略", "polarity": 1, "strength": 0.9}]},
    ]})
    orch = L2Orchestrator(ModelGateway(provider=provider), session)
    assert orch.scan(user_id="u1").claims_extracted == 1

    repo = KnowledgeRepository(session, user_id="u1")
    repo.apply_update(item, content="彻底改写后的新观点内容")
    session.commit()
    session.refresh(item)  # 取回数据库端 updated_at

    assert orch.scan(user_id="u1").claims_extracted == 1
    claims = ClaimRepository(session, user_id="u1").list_by_item(item.id)
    assert [c.statement for c in claims] == ["新主张"]


def test_extraction_failure_leaves_for_retry(session):
    """提取失败不置位 claims_scanned_at：下轮扫描自动重试，不丢条目。"""
    item = _ingest(session, "u1", "长期主义", "坚守战略。")
    provider = FakeProvider({"batch_extraction": ["这不是 JSON {{{"]} )
    orch = L2Orchestrator(ModelGateway(provider=provider), session)

    result = orch.scan(user_id="u1")
    assert result.extraction_failures == 1
    assert result.claims_extracted == 0
    session.refresh(item)
    assert item.claims_scanned_at is None  # 未置位，下轮重试

    provider.rows_by_task["batch_extraction"] = [{"claims": [
        {"statement": "补跑成功的主张", "topic": "决策", "polarity": 1, "strength": 0.8},
    ]}]
    assert orch.scan(user_id="u1").claims_extracted == 1


def test_scan_empty_library(session):
    result = _orchestrator(session, {}).scan(user_id="u1")
    assert result.scanned_items == 0
    assert result.pairs_judged == 0
    assert result.conflicts_found == 0


# ---------- L2-3~6 候选对 / 判定 / 阈值 / 抑制 ----------


def _extraction_rows() -> dict[str, list]:
    """两条「长期主义 vs 敏捷」条目的主张提取预案（纯数据，不写库）。"""
    return {
        "batch_extraction": [
            {"claims": [{"statement": "应坚守既定战略至少三年", "topic": "决策策略", "polarity": 1, "strength": 0.9}]},
            {"claims": [{"statement": "应根据市场反馈每月调整方向", "topic": "决策策略", "polarity": -1, "strength": 0.9}]},
        ],
    }


def _two_same_topic_items(session):
    _ingest(session, "u1", "长期主义者的决策框架", "坚守既定战略。")
    _ingest(session, "u1", "敏捷思维", "根据反馈快速掉头。")
    return _extraction_rows()


def test_conflict_created_and_idempotent(session):
    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = [{
        "relation": "矛盾", "conflict_type": "立场对立",
        "detail": "A 主张坚守三年，B 主张每月掉头", "suggestion": "生成决策哲学对比表", "confidence": 0.9,
    }]
    orch = _orchestrator(session, provider_rows)

    first = orch.scan(user_id="u1")
    assert first.conflicts_found == 1
    conflicts = ConflictRepository(session, user_id="u1").list_by_user("u1")
    assert len(conflicts) == 1
    assert conflicts[0].conflict_type == "立场对立"
    assert conflicts[0].user_state == "unseen"

    # ADR-13 幂等：重扫同一对不再重复判定、不再入库
    second = orch.scan(user_id="u1")
    assert second.pairs_judged == 0
    assert second.conflicts_found == 0
    assert len(ConflictRepository(session, user_id="u1").list_by_user("u1")) == 1


def test_non_conflict_relations_not_stored(session):
    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = [{
        "relation": "互补", "conflict_type": "", "detail": "时间尺度不同", "suggestion": "", "confidence": 0.85,
    }]
    result = _orchestrator(session, provider_rows).scan(user_id="u1")
    assert result.conflicts_found == 0
    assert result.pairs_judged == 1


def test_low_confidence_discarded(session):
    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = [{
        "relation": "矛盾", "conflict_type": "立场对立", "detail": "拿不准",
        "suggestion": "", "confidence": 0.3,
    }]
    result = _orchestrator(session, provider_rows).scan(user_id="u1")
    assert result.conflicts_found == 0  # 低于 0.5 直接丢弃（架构 §7.3）


def test_suppression_after_repeated_ignores(session):
    """同类型冲突被忽略 ≥3 次后不再产生该类推荐（UC-L2-03）。"""
    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = [{
        "relation": "矛盾", "conflict_type": "立场对立", "detail": "d", "suggestion": "s", "confidence": 0.9,
    }]
    orch = _orchestrator(session, provider_rows)

    xrepo = ConflictRepository(session, user_id="u1")
    for i in range(3):  # 制造 3 条已忽略的同类型冲突（占位对，不与本判定对重合）
        row = xrepo.create(
            user_id="u1", item_a_id="pa", item_b_id="pb",
            claim_a_id=f"ca{i}", claim_b_id=f"cb{i}",
            conflict_type="立场对立",
        )
        xrepo.set_state(row, "ignored")
    session.commit()

    result = orch.scan(user_id="u1")
    assert result.conflicts_found == 0
    assert result.conflicts_suppressed == 1


def test_judgment_failure_skips_pair(session):
    """判定解析失败：该对不入库、不中断扫描，下轮重试。"""
    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = ["broken json {"]
    result = _orchestrator(session, provider_rows).scan(user_id="u1")
    assert result.conflicts_found == 0
    assert result.pairs_judged == 0
    assert len(ConflictRepository(session, user_id="u1").list_by_user("u1")) == 0


def test_pairs_capped_per_scan(session):
    """候选对数超过单次上限时截断（成本闸门）。"""
    from app.core.config import settings

    for i in range(8):  # 同 topic 8 条主张（分属不同条目）→ 28 对
        _ingest(session, "u1", f"条目{i}", f"内容{i}")
    orch = _orchestrator(session, {
        "batch_extraction": [
            {"claims": [{"statement": f"主张{i}", "topic": "同一主题", "polarity": 1, "strength": 0.9}]}
            for i in range(8)
        ],
    })
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert len(claims) == 8

    pairs = orch._candidate_pairs(claims)
    assert len(pairs) == settings.l2_max_pairs_per_scan


# ---------- L2-2 语义近邻通道 ----------


class StubEmbedding(EmbeddingModel):
    """按 statement → 预设向量回放，精确控制余弦距离，不依赖具体 embedding 实现。"""

    dim = 2

    def __init__(self, vec_by_statement: dict[str, list[float]]):
        self._vecs = vec_by_statement

    def embed(self, texts):
        return [list(self._vecs[t]) for t in texts]


def test_semantic_channel_pairs_different_topics(session):
    """主题标签措辞不一致时，语义近邻通道兜底组对（真实场景：LLM 标签不稳定）。"""
    _ingest(session, "u1", "长期主义", "坚守战略。")
    _ingest(session, "u1", "敏捷思维", "快速掉头。")
    orch = _orchestrator(session, {
        "batch_extraction": [
            {"claims": [{"statement": "主张甲", "topic": "战略定力", "polarity": 1, "strength": 0.9}]},
            {"claims": [{"statement": "主张乙", "topic": "快速调整", "polarity": -1, "strength": 0.9}]},
        ],
    })
    orch.scan(user_id="u1")  # 先落主张（判定走默认「无关」，不产生冲突）
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert len(claims) == 2
    vecs = {c.statement: v for c, v in zip(claims, ([1.0, 0.0], [0.8, 0.6]))}  # cos≈0.8，带内
    orch._embedding = StubEmbedding(vecs)
    pairs = orch._candidate_pairs(claims)
    assert len(pairs) == 1


def test_semantic_channel_excludes_out_of_band(session):
    """语义距离带两端排除：太近≈重复，太远≈无关（架构 §7.2）。"""
    _ingest(session, "u1", "条目A", "内容A")
    _ingest(session, "u1", "条目B", "内容B")
    _ingest(session, "u1", "条目C", "内容C")
    orch = _orchestrator(session, {})
    claims = [
        Claim(knowledge_item_id="ia", statement="主张A", topic="索引"),
        Claim(knowledge_item_id="ib", statement="主张B", topic="健身"),   # 与 A 无关
        Claim(knowledge_item_id="ic", statement="主张C", topic="查询"),   # 与 A 近似重复
    ]
    orch._embedding = StubEmbedding({
        "主张A": [1.0, 0.0],
        "主张B": [0.0, 1.0],   # sim=0 → 太远
        "主张C": [1.0, 0.0],   # sim=1.0 → 太近
    })
    pairs = orch._candidate_pairs(claims)
    assert pairs == []


def test_embedding_unavailable_falls_back_to_topic(session):
    """向量模型不可用时退化为纯 topic 通道，扫描不中断。"""
    class BrokenEmbedding(EmbeddingModel):
        dim = 8

        def embed(self, texts):
            raise RuntimeError("模型不可用")

    provider_rows = _two_same_topic_items(session)
    provider_rows["conflict_detection"] = [{
        "relation": "矛盾", "conflict_type": "立场对立",
        "detail": "d", "suggestion": "s", "confidence": 0.9,
    }]
    orch = L2Orchestrator(
        ModelGateway(provider=FakeProvider(provider_rows)), session,
        embedding=BrokenEmbedding(),
    )
    result = orch.scan(user_id="u1")
    assert result.conflicts_found == 1  # topic 通道仍组对并完成判定


# ---------- 主张向量的复用与回写 ----------


class CountingEmbedding(EmbeddingModel):
    """记录 embed 调用次数与收到的文本，回放固定向量。

    `dim` 与 HashEmbedding 对齐（256），这样测试能自由选择「库内向量可复用」
    还是「维度不符需重算」两种场景。
    """

    dim = 256

    def __init__(self, vec: list[float] | None = None):
        self.calls = 0
        self.seen: list[list[str]] = []
        self._vec = vec or [1.0] + [0.0] * 255

    def embed(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        return [list(self._vec) for _ in texts]


def test_similarities_reuse_stored_claim_vectors(session):
    """已落库的主张向量直接复用：相似度计算不再触碰 embedding 模型。"""
    provider_rows = _two_same_topic_items(session)
    orch = _orchestrator(session, provider_rows)
    orch.scan(user_id="u1")  # 扫描时已把向量写进 Claim.embedding
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert len(claims) == 2
    assert all(c.embedding for c in claims)

    counter = CountingEmbedding()
    orch._embedding = counter
    sims = orch._claim_similarities(claims)

    assert counter.calls == 0  # 全部命中库内向量 → 零模型调用
    assert len(sims) == 1


def test_similarities_recompute_and_write_back_missing_vectors(session):
    """向量缺失时批量补算并回写：第二次计算不再重复支付 embedding 成本。"""
    provider_rows = _two_same_topic_items(session)
    orch = _orchestrator(session, provider_rows)
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    for c in claims:  # 模拟历史数据：主张在、向量不在
        c.embedding = None
    session.commit()

    counter = CountingEmbedding()
    orch._embedding = counter
    orch._claim_similarities(claims)

    assert counter.calls == 1  # 两条缺失合并为一次批量调用
    assert all(c.embedding for c in claims)  # 补算结果已回写

    session.expire_all()  # 重新从库里读，确认真的落盘而不是只在内存
    reloaded = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert all(c.embedding for c in reloaded)

    orch._claim_similarities(reloaded)
    assert counter.calls == 1  # 第二次全部命中，不再调用


def test_vectors_with_mismatched_dimension_are_recomputed(session):
    """换过 embedding 模型后旧维度向量不可复用（不同向量空间），按当前模型重算覆盖。"""
    provider_rows = _two_same_topic_items(session)
    orch = _orchestrator(session, provider_rows)
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    for c in claims:  # 2 维旧向量；当前模型是 256 维
        c.embedding = EmbeddingModel.dumps([1.0, 0.0])
    session.commit()

    counter = CountingEmbedding()
    orch._embedding = counter
    orch._claim_similarities(claims)

    assert counter.calls == 1
    assert all(len(EmbeddingModel.loads(c.embedding)) == 256 for c in claims)


def test_scan_does_not_re_embed_claims_for_pairing(session):
    """扫描内部：候选对生成复用刚落库的向量，不为组对再算一遍。

    这是本项优化的直接收益——组对阶段从「每次扫描重算全部主张」变为零模型调用。
    """
    _ingest(session, "u1", "条目A", "内容A")
    _ingest(session, "u1", "条目B", "内容B")
    counter = CountingEmbedding()
    orch = L2Orchestrator(
        ModelGateway(provider=FakeProvider(_extraction_rows())), session, embedding=counter,
    )
    orch.scan(user_id="u1")

    assert counter.calls == 2  # 两个条目各一次；组对阶段零调用
    pairs = orch._candidate_pairs(ClaimRepository(session, user_id="u1").list_by_user("u1"))
    assert len(pairs) == 1
    assert counter.calls == 2  # 再组一次对，仍不触碰模型


def test_partial_vectors_keep_semantic_channel(session):
    """部分主张缺向量且补算失败时，已有向量仍产出相似度——不整体退化。"""
    class BrokenEmbedding(EmbeddingModel):
        dim = 256

        def embed(self, texts):
            raise RuntimeError("模型不可用")

    for i in range(3):
        _ingest(session, "u1", f"条目{i}", f"内容{i}")
    orch = _orchestrator(session, {
        "batch_extraction": [
            {"claims": [{"statement": f"主张{i}", "topic": f"主题{i}", "polarity": 1, "strength": 0.8}]}
            for i in range(3)
        ],
    })
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert len(claims) == 3

    # 按 statement 定位而不是按下标：三条主张同批创建，created_at 可能同秒，排序不稳
    by_stmt = {c.statement: c for c in claims}
    a, b, missing_one = by_stmt["主张0"], by_stmt["主张1"], by_stmt["主张2"]
    fixed = EmbeddingModel.dumps([1.0] + [0.0] * 255)
    a.embedding = fixed
    b.embedding = fixed
    missing_one.embedding = None  # 缺失且补算会失败
    session.commit()

    orch._embedding = BrokenEmbedding()
    sims = orch._claim_similarities(claims)

    assert len(sims) == 1  # 只有两条有向量的主张构成的那一对
    assert sims[(a.id, b.id)] == pytest.approx(1.0)


def test_scan_and_similarity_share_one_embed_text_rule(session):
    """扫描侧与相似度侧送入 embedding 的文本必须逐字相同（同一口径）。

    分叉的后果不是报错而是静默失真：已落库的向量与新算的向量落在不同文本上，
    余弦照旧算得出来，但它度量的已不是同一个东西。
    """
    from app.agent.l2_orchestrator import _MAX_CLAIM_STATEMENT

    _ingest(session, "u1", "长期主义", "坚守战略。")
    _ingest(session, "u1", "敏捷思维", "快速掉头。")
    recorder = CountingEmbedding()
    orch = L2Orchestrator(
        ModelGateway(provider=FakeProvider(_extraction_rows())), session, embedding=recorder,
    )
    orch.scan(user_id="u1")
    claims = ClaimRepository(session, user_id="u1").list_by_user("u1")
    assert len(claims) == 2

    scan_texts = [t for batch in recorder.seen for t in batch]
    assert sorted(scan_texts) == sorted(c.statement for c in claims)  # 落库 statement 即嵌入文本

    for c in claims:  # 清空后重算：相似度侧应送入完全相同的文本
        c.embedding = None
    session.commit()
    orch._claim_similarities(claims)

    assert sorted(recorder.seen[-1]) == sorted(scan_texts)

    # 口径本身：去首尾空白 + 截断，只在一处定义
    long_text = "  " + "甲" * (_MAX_CLAIM_STATEMENT + 50) + "  "
    text = orch._claim_embed_text(long_text)
    assert len(text) == _MAX_CLAIM_STATEMENT
    assert not text.startswith(" ")
    assert orch._claim_embed_text(None) == ""


# ---------- API ----------


def test_api_scan_list_and_feedback(client):
    # API 场景：条目经 HTTP 录入；提取预案按两次录入各给一份
    fake = FakeProvider({
        "batch_extraction": _extraction_rows()["batch_extraction"],
        "conflict_detection": [{
            "relation": "矛盾", "conflict_type": "立场对立",
            "detail": "A 主张坚守三年，B 主张每月掉头", "suggestion": "生成对比表", "confidence": 0.9,
        }],
    })
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=fake)
    try:
        headers_a = auth_headers(client, "l2_api_a")
        client.post("/api/v1/knowledge/items",
                    json={"title": "长期主义者的决策框架", "content": "坚守既定战略。"}, headers=headers_a)
        client.post("/api/v1/knowledge/items",
                    json={"title": "敏捷思维", "content": "根据反馈快速掉头。"}, headers=headers_a)

        scan = client.post("/api/v1/l2/scan", headers=headers_a)
        assert scan.status_code == 200, scan.text
        assert scan.json()["conflicts_found"] == 1

        listed = client.get("/api/v1/l2/conflicts", headers=headers_a).json()
        assert listed["total"] == 1
        row = listed["items"][0]
        assert row["title_a"] == "长期主义者的决策框架"
        assert row["title_b"] == "敏捷思维"
        assert "坚守" in row["claim_a"]
        assert row["user_state"] == "unseen"

        conflict_id = row["conflict_id"]
        patched = client.patch(
            f"/api/v1/l2/conflicts/{conflict_id}/state",
            json={"state": "ignored"}, headers=headers_a,
        )
        assert patched.status_code == 200
        assert patched.json()["user_state"] == "ignored"
        assert client.get("/api/v1/l2/conflicts?state=ignored", headers=headers_a).json()["total"] == 1
        assert client.get("/api/v1/l2/conflicts?state=unseen", headers=headers_a).json()["total"] == 0
        assert client.patch(
            f"/api/v1/l2/conflicts/{conflict_id}/state",
            json={"state": "bogus"}, headers=headers_a,
        ).status_code == 422

        # 越权：B 用户不可见也不可改 A 的冲突
        headers_b = auth_headers(client, "l2_api_b")
        assert client.get("/api/v1/l2/conflicts", headers=headers_b).json()["total"] == 0
        assert client.patch(
            f"/api/v1/l2/conflicts/{conflict_id}/state",
            json={"state": "accepted"}, headers=headers_b,
        ).status_code == 403

        # B 扫描自己的空库：互不串扰
        scan_b = client.post("/api/v1/l2/scan", headers=headers_b)
        assert scan_b.json()["conflicts_found"] == 0
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)


def test_api_scan_requires_auth(client):
    assert client.post("/api/v1/l2/scan").status_code == 401


# ---------- 书籍通读笔记参与冲突检测 ----------


def _seed_reading(session, user_id: str, *, book_title: str) -> str:
    """造一本书 + 一份通读笔记，返回书的 source 键（book:<id>）。"""
    from app.domain.models.book_agent_reading import BookAgentReading
    from app.domain.repositories.book_repository import BookRepository

    book = BookRepository(session, user_id=user_id).create(
        user_id=user_id, title=book_title, author="", format="txt", file_path="",
        chapters=[], full_text="", total_chars=0,
    )
    session.add(BookAgentReading(
        user_id=user_id, book_id=book.id, status="done", total_chars=100,
        summary="总评",
        chapters_note=[{
            "index": 0, "title": "第一章", "gist": "总括",
            "points": ["一万小时练习即可成就专家", "天赋在技能习得中的作用可以忽略"],
        }],
    ))
    session.commit()
    return f"book:{book.id}"


def test_scan_includes_book_reading_claims(session):
    """通读笔记的要点以影子主张进漏斗：书观点与笔记观点能被判出冲突。"""
    from app.agent.conflict_view import conflict_views

    # 书名与笔记主张的 topic 相同 → 走 topic 通道稳定配对（hash embedding 的语义带不可控）
    source = _seed_reading(session, "u1", book_title="技能习得")
    _ingest(session, "u1", "天赋论", "没有天赋苦练也没用。")
    orch = _orchestrator(session, {
        "batch_extraction": [{
            "claims": [
                {"statement": "没有天赋苦练也没用", "topic": "技能习得", "polarity": -1, "strength": 0.9},
            ]
        }],
        "conflict_detection": [{
            "relation": "矛盾", "conflict_type": "立场对立",
            "detail": "一边说天赋决定上限，一边说练习即可成就专家",
            "suggestion": "用同一项技能做一次对照实验", "confidence": 0.9,
        }],
    })

    result = orch.scan(user_id="u1")

    assert result.book_readings_used == 1
    book_claims = ClaimRepository(session, user_id="u1").list_by_item(source)
    assert len(book_claims) == 2, "通读笔记的要点应转成影子主张"
    assert result.conflicts_found == 1
    conflict = ConflictRepository(session, user_id="u1").list_by_user("u1")[0]
    assert source in (conflict.item_a_id, conflict.item_b_id)

    views = conflict_views(session, user_id="u1", conflict_ids=[conflict.id])
    assert views[0]["title_a"].endswith("通读笔记") or views[0]["title_b"].endswith("通读笔记")


def test_book_claims_rebuilt_only_when_reading_updated(session):
    """笔记没变 → 主张原样保留（不重复向量化）；笔记重读刷新后 → 重建。"""
    from datetime import datetime, timedelta

    source = _seed_reading(session, "u2", book_title="技能习得")
    orch = _orchestrator(session, {})

    orch.scan(user_id="u2")
    first_ids = [c.id for c in ClaimRepository(session, user_id="u2").list_by_item(source)]
    assert first_ids, "首次扫描应建出影子主张"

    orch.scan(user_id="u2")  # 笔记没变：主张原样复用
    second_ids = [c.id for c in ClaimRepository(session, user_id="u2").list_by_item(source)]
    assert second_ids == first_ids

    reading = session.scalars(
        __import__("sqlalchemy").select(__import__(
            "app.domain.models.book_agent_reading", fromlist=["BookAgentReading"]
        ).BookAgentReading)
    ).first()
    reading.updated_at = datetime.utcnow().replace(tzinfo=None) + timedelta(seconds=10)
    session.commit()

    orch.scan(user_id="u2")  # 笔记更新：delete + create 重建
    third_ids = [c.id for c in ClaimRepository(session, user_id="u2").list_by_item(source)]
    assert third_ids and third_ids != first_ids


def test_judgment_logs_persist_and_echoes_collected(session):
    """每一对送判主张都落判定日志（含非矛盾）；书×笔的互补对收集为「印证」。"""
    from app.domain.repositories.l2_judgment_log_repository import L2JudgmentLogRepository

    source = _seed_reading(session, "u3", book_title="技能习得")
    _ingest(session, "u3", "天赋论", "没有天赋苦练也没用。")
    orch = _orchestrator(session, {
        "batch_extraction": [{
            "claims": [
                {"statement": "没有天赋苦练也没用", "topic": "技能习得", "polarity": -1, "strength": 0.9},
            ]
        }],
        "conflict_detection": [
            {"relation": "互补", "detail": "书与笔记互相支撑", "confidence": 0.8},
            {"relation": "互补", "detail": "同一结论的另一面", "confidence": 0.7},
        ],
    })

    result = orch.scan(user_id="u3")

    logs = L2JudgmentLogRepository(session, user_id="u3").list_recent("u3")
    assert len(logs) == result.pairs_judged > 0
    assert all(log.relation == "互补" for log in logs)
    assert all("通读笔记" in (log.title_a + log.title_b) for log in logs)
    assert len(result.echoes) == 2, "两条跨来源互补对都应收集为印证"
    assert result.echoes[0]["title_a"].endswith("通读笔记") or \
        result.echoes[0]["title_b"].endswith("通读笔记")


def test_judgment_logs_not_written_for_llm_failure(session):
    """判定解析失败时不写日志（该对下轮重试，日志只记真实发生过的判定）。"""
    from app.domain.repositories.l2_judgment_log_repository import L2JudgmentLogRepository

    source = _seed_reading(session, "u4", book_title="技能习得")
    _ingest(session, "u4", "天赋论", "没有天赋苦练也没用。")
    orch = _orchestrator(session, {
        "batch_extraction": [{
            "claims": [
                {"statement": "没有天赋苦练也没用", "topic": "技能习得", "polarity": -1, "strength": 0.9},
            ]
        }],
        "conflict_detection": ["不是 JSON", "不是 JSON"],
    })
    result = orch.scan(user_id="u4")
    assert result.pairs_judged == 0
    assert L2JudgmentLogRepository(session, user_id="u4").list_recent("u4") == []


def test_api_scan_requires_auth(client):
    assert client.post("/api/v1/l2/scan").status_code == 401