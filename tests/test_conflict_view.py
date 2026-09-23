"""共享冲突视图模块（app/agent/conflict_view.py）测试。

这个模块把三件事收成一份：一条冲突长什么样（视图）、哪处最该处理（top_unresolved）、
怎么拼给模型看（unresolved_section）。它们原先分散在 turns 与各 orchestrator 里，
一旦各写各的就会出现「开场说的是 A、简报问的是 B」这种静默分叉——所以逐条钉住。
"""
from __future__ import annotations

from app.agent import conflict_view
from app.agent.conflict_view import (
    ConflictBrief,
    conflict_views,
    top_unresolved,
    unresolved_section,
)
from app.domain.repositories.conflict_repository import ConflictRepository
from tests.conflict_fixtures import make_conflict

USER = "u1"


def test_views_carry_claim_ids(session):
    """视图必须带上主张 id——下游要靠它取已落库的向量判断语义相关性。"""
    conflict, _item_a, _item_b, claim_a, claim_b = make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )
    views = conflict_views(session, user_id=USER, conflict_ids=[conflict.id])

    assert len(views) == 1
    view = views[0]
    assert view["claim_a_id"] == claim_a.id
    assert view["claim_b_id"] == claim_b.id
    assert view["title_a"] == "索引·甲"
    assert view["claim_a"] == "B+树更适合范围查询"


def test_top_unresolved_picks_highest_confidence(session):
    """「最该处理的一处」= 未处理冲突里置信度最高的那条。"""
    _low, *_ = make_conflict(
        session, USER, tag="低", statement_a="A1", statement_b="B1", confidence=0.6
    )
    high, *_ = make_conflict(
        session, USER, tag="高", statement_a="A2", statement_b="B2", confidence=0.9
    )

    picked = top_unresolved(session, user_id=USER, limit=1)
    assert [b.conflict_id for b in picked] == [high.id]
    assert isinstance(picked[0], ConflictBrief)
    assert picked[0].confidence == 0.9
    assert picked[0].claim_a_id  # 带上主张 id，供 L4 取向量


def test_top_unresolved_skips_handled(session):
    """用户已经处理过（采纳/忽略）的冲突不该再出现在「最该处理」里。"""
    conflict, *_ = make_conflict(session, USER, tag="已忽略", statement_a="A", statement_b="B")
    ConflictRepository(session, user_id=USER).set_state(conflict, "ignored")
    session.commit()

    assert top_unresolved(session, user_id=USER, limit=3) == []


def test_top_unresolved_is_user_scoped(session):
    """别人的冲突不能出现在我的开场里。"""
    make_conflict(session, USER, tag="我的", statement_a="A", statement_b="B")
    assert top_unresolved(session, user_id="someone-else", limit=3) == []


def test_section_contains_both_sides(session):
    """拼给模型的文本要同时含双方书名与主张——否则模型无从判断矛盾在哪一点上。"""
    make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )
    section = unresolved_section(session, user_id=USER, limit=3)

    assert "B+树更适合范围查询" in section
    assert "哈希索引更适合范围查询" in section
    assert "索引·甲" in section


def test_section_is_empty_without_conflicts(session):
    """没有未解冲突就返回空串——调用方直接拼接，空串即「没有这段」。"""
    assert unresolved_section(session, user_id=USER, limit=3) == ""


def test_section_swallows_read_errors(session, monkeypatch):
    """读冲突失败要返回空串而不是抛错：注入冲突是增强，不该拖垮主任务。"""
    def boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(conflict_view, "top_unresolved", boom)
    assert unresolved_section(session, user_id=USER, limit=3) == ""
