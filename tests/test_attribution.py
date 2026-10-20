"""D 模块测试：冲突归因（七项校验 / 幂等 / 惰性触发）。

对应 `docs/验收自检与修正-2026-10-20.md` 修正 4（补「背景」字段）与
总方案 D 模块验收标准 AT-D1~D7。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent import attribution
from app.agent.attribution import AttributionDraft, ProbeDraft, SceneGuideItem
from app.api import deps
from app.domain.repositories.conflict_repository import ConflictRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.conflict_fixtures import make_conflict
from tests.helpers import sign_in


class _Scripted(LLMProvider):
    def __init__(self, rows: list[str] | None = None):
        self.rows = list(rows or [])
        self.calls: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(task_type)
        text = self.rows.pop(0) if self.rows else "{}"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1,
            finish_reason="stop", model=model, reasoning=reasoning,
        )


def _good_draft(**over) -> AttributionDraft:
    base = dict(
        cause_type="适用边界",
        background_a="作者是数据库内核工程师，写作对象是要做存储选型的后端团队。",
        background_b="作者来自 OLTP 高并发场景，长期处理点查与短事务。",
        premise_a="查询以范围扫描与排序为主。",
        premise_b="查询几乎都是单点等值命中。",
        boundary_a="一次查询要返回一个区间时成立。",
        boundary_b="只按键取单行、没有范围条件时成立。",
        scene_guidance=[
            SceneGuideItem(scene="按时间区间翻页", follow="a", reason="区间扫描是它的强项"),
            SceneGuideItem(scene="单键高频读取", follow="b", reason="哈希是 O(1)"),
        ],
        probe=ProbeDraft(question="慢查询是范围多还是等值多？", how_to_check="统计近一周慢查询的 WHERE 形态"),
    )
    base.update(over)
    return AttributionDraft(**base)


def _conflict(session, *, confidence=0.8):
    user_id = "u1"
    return make_conflict(
        session, user_id, tag="索引",
        statement_a="B+树更适合范围查询", statement_b="哈希索引更适合范围查询",
        confidence=confidence,
    )


# ---- 正常路径 ---------------------------------------------------------------


def test_attribute_ok_and_persists(session):
    conflict, *_ = _conflict(session)
    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id,
        context={"draft": _good_draft()},
    )
    assert outcome.state == "ok"
    assert outcome.attribution["cause_type"] == "适用边界"
    assert outcome.attribution["background_a"]
    assert len(outcome.scene_guidance) == 2

    session.expire_all()
    row = ConflictRepository(session, user_id="u1").get(conflict.id)
    assert row.attributed_at is not None
    assert row.attribution["background_b"]
    assert row.probe["how_to_check"]


def test_scene_guidance_follow_values_preserved(session):
    conflict, *_ = _conflict(session)
    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": _good_draft()}
    )
    assert {g["follow"] for g in outcome.scene_guidance} == {"a", "b"}


# ---- 幂等与惰性 -------------------------------------------------------------


def test_already_attributed_does_not_call_model(session):
    conflict, *_ = _conflict(session)
    attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": _good_draft()}
    )

    provider = _Scripted()
    again = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id,
        gateway=ModelGateway(provider=provider),
    )
    assert again.state == "already"
    assert provider.calls == []


def test_force_reruns(session):
    conflict, *_ = _conflict(session)
    attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": _good_draft()}
    )
    provider = _Scripted()
    again = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id,
        gateway=ModelGateway(provider=provider), force=True,
        context={"draft": _good_draft(cause_type="证据来源")},
    )
    assert again.state == "ok"
    assert again.attribution["cause_type"] == "证据来源"


def test_low_confidence_is_skipped(session):
    """置信度太低不做归因：解释一处可能并不存在的矛盾只会放大误判。"""
    conflict, *_ = _conflict(session, confidence=0.4)
    provider = _Scripted()
    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id,
        gateway=ModelGateway(provider=provider),
    )
    assert outcome.state == "skipped"
    assert provider.calls == []


def test_unknown_conflict(session):
    outcome = attribution.attribute_conflict(session, user_id="u1", conflict_id="nope")
    assert outcome.state == "not_found"


def test_other_users_conflict_is_not_found(session):
    conflict, *_ = _conflict(session)
    outcome = attribution.attribute_conflict(session, user_id="u2", conflict_id=conflict.id)
    assert outcome.state == "not_found"


# ---- 七项校验（缺一即不落库）------------------------------------------------


def _assert_rejected(session, draft, *, keyword=""):
    conflict, *_ = _conflict(session)
    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": draft}
    )
    assert outcome.state == "degraded"
    if keyword:
        assert keyword in outcome.note
    session.expire_all()
    row = ConflictRepository(session, user_id="u1").get(conflict.id)
    assert row.attributed_at is None, "不合格的归因不能落库"


def test_missing_background_rejected(session):
    _assert_rejected(session, _good_draft(background_a="背景不同"), keyword="背景")


def test_overlong_background_rejected(session):
    """背景写成一段分析而不是一句判断 → 不落库。

    真机产出过 150+ 字的背景：前半段是「题面出处为…未提供作者信息…不能补作者生平」，
    用户要读三行才看到有用那半句。可判定的（长度）就该由代码拦，不靠提示词自觉。
    """
    from app.agent.attribution import MAX_BACKGROUND_LEN

    _assert_rejected(
        session,
        _good_draft(background_a="作" * (MAX_BACKGROUND_LEN + 10)),
        keyword="背景",
    )


def test_background_length_ceiling_is_documented():
    from app.agent.attribution import MAX_BACKGROUND_LEN, MIN_BACKGROUND_LEN

    assert MIN_BACKGROUND_LEN < MAX_BACKGROUND_LEN
    # 一句判断的合理长度：太短是套话，太长是分析报告
    assert 60 <= MAX_BACKGROUND_LEN <= 200


def test_missing_premise_rejected(session):
    _assert_rejected(session, _good_draft(premise_b=""), keyword="前提")


def test_missing_boundary_rejected(session):
    _assert_rejected(session, _good_draft(boundary_a=""), keyword="边界")


def test_too_few_scenes_rejected(session):
    _assert_rejected(
        session,
        _good_draft(scene_guidance=[
            SceneGuideItem(scene="只给一个场景", follow="a", reason="理由")
        ]),
        keyword="场景",
    )


def test_bad_follow_value_rejected(session):
    _assert_rejected(
        session,
        _good_draft(scene_guidance=[
            SceneGuideItem(scene="场景一", follow="c", reason="理由"),
            SceneGuideItem(scene="场景二", follow="a", reason="理由"),
        ]),
        keyword="follow",
    )


def test_missing_probe_rejected(session):
    _assert_rejected(session, _good_draft(probe=ProbeDraft()), keyword="判据")


def test_unknown_cause_type_rejected(session):
    _assert_rejected(session, _good_draft(cause_type="玄学"), keyword="原因类型")


def test_unparseable_output_degrades(session):
    conflict, *_ = _conflict(session)
    provider = _Scripted(["不是 JSON"])
    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id,
        gateway=ModelGateway(provider=provider),
    )
    assert outcome.state == "degraded"
    assert provider.calls == ["conflict_attribution"]


def test_book_source_conflict_can_be_attributed(session):
    """「书 vs 笔记」的冲突里，书那一侧是 `book:<id>` 影子来源。

    回归：原实现拿 `book:<id>` 去查 knowledge_items，必然查不到，
    于是所有书侧冲突都被判成"原文缺失"跳过——用户永远拿不到归因，
    而且看到的原因是错的（说缺原文，其实原文就在通读笔记里）。
    """
    from app.domain.models.book import Book
    from app.domain.repositories.claim_repository import ClaimRepository
    from app.domain.repositories.conflict_repository import ConflictRepository
    from app.domain.repositories.knowledge_repository import KnowledgeRepository

    book = Book(
        user_id="u1", title="原则", author="", format="txt", file_path="x.txt",
        full_text="x" * 100, total_chars=100,
    )
    session.add(book)
    session.commit()

    item_b = KnowledgeRepository(session, user_id="u1").create(
        user_id="u1", title="我的笔记", content="极度透明在团队里做不到。"
    )
    crepo = ClaimRepository(session, user_id="u1")
    claim_a = crepo.create(
        user_id="u1", knowledge_item_id=f"book:{book.id}",
        statement="极度透明是组织高效的前提", topic="管理",
    )
    claim_b = crepo.create(
        user_id="u1", knowledge_item_id=item_b.id,
        statement="极度透明在团队里做不到", topic="管理",
    )
    conflict = ConflictRepository(session, user_id="u1").create(
        user_id="u1",
        item_a_id=f"book:{book.id}", item_b_id=item_b.id,
        claim_a_id=claim_a.id, claim_b_id=claim_b.id,
        conflict_type="立场对立", confidence=0.8,
    )
    session.commit()

    outcome = attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": _good_draft()}
    )
    assert outcome.state == "ok", outcome.note


# ---- 视图与端点 -------------------------------------------------------------


def test_conflict_views_exposes_flag(session):
    from app.agent.conflict_view import conflict_views

    conflict, *_ = _conflict(session)
    view = conflict_views(session, user_id="u1", conflict_ids=[conflict.id])[0]
    assert view["has_attribution"] is False

    attribution.attribute_conflict(
        session, user_id="u1", conflict_id=conflict.id, context={"draft": _good_draft()}
    )
    view = conflict_views(session, user_id="u1", conflict_ids=[conflict.id])[0]
    assert view["has_attribution"] is True


def test_endpoint_attribute(client, session):
    user = sign_in(client, "attr_api")
    conflict, *_ = make_conflict(
        session, user.user_id, tag="索引",
        statement_a="B+树更适合范围查询", statement_b="哈希索引更适合范围查询",
        confidence=0.8,
    )
    provider = _Scripted(['{"cause_type":"适用边界"}'])
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=provider)
    try:
        resp = client.post(
            f"/api/v1/l2/conflicts/{conflict.id}/attribute", headers=user.headers
        )
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conflict_id"] == conflict.id
    # 模型输出不合格 → degraded，但仍返回 200（前端据此提示"没分析出来"）
    assert body["state"] == "degraded"


def test_endpoint_404_for_missing(client, session):
    user = sign_in(client, "attr_404")
    resp = client.post("/api/v1/l2/conflicts/nope/attribute", headers=user.headers)
    assert resp.status_code == 404
