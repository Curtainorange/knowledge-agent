"""L2 冲突检测基础（df377ec）回归测试。

`Conflict` 模型文件就位但 `app/domain/models/__init__.py` 未导入时，SQLAlchemy
metadata 不注册该表——`init_db()` 与 alembic 都跳过，导致 `ConflictRepository`
首次写入就撞 `OperationalError: no such table: conflicts`。本测试锁定：
1. `conflicts` 表必须在 Base.metadata 中（防回归 __init__.py 漏 import）
2. `ConflictRepository` 真实可写可查（防仓储内部或迁移链路断裂）
"""
from __future__ import annotations

from app.domain.models.base import Base
from app.domain.models.conflict import Conflict
from app.domain.repositories.conflict_repository import ConflictRepository, make_pair_key


def test_conflicts_table_is_registered():
    """冲突表必须被 SQLAlchemy metadata 注册（init_db / alembic 都靠这张清单）。"""
    names = {t.name for t in Base.metadata.sorted_tables}
    assert "conflicts" in names, (
        "conflicts 表未注册——多半是 app/domain/models/__init__.py 漏了 import"
    )
    assert "claims" in names, "claims 表未注册——同上"


def test_make_pair_key_is_order_invariant():
    """同一对主张无论先后顺序都映射到同一个 pair_key，避免重复入库。"""
    a, b = "claim-aaa", "claim-bbb"
    assert make_pair_key(a, b) == make_pair_key(b, a)


def test_conflict_repo_create_and_find_by_pair(session):
    """通过仓储真实写入一条冲突并按 pair_key 找回。"""
    repo = ConflictRepository(session, user_id="u1")
    created = repo.create(
        user_id="u1",
        item_a_id="item-A",
        item_b_id="item-B",
        claim_a_id="claim-A",
        claim_b_id="claim-B",
        conflict_type="逻辑矛盾",
        detail="A 主张 X，B 主张非 X",
        suggestion="补一张决策哲学对比表",
        confidence=0.82,
    )
    session.flush()

    again = repo.find_by_pair("u1", created.pair_key)
    assert again is not None
    assert again.id == created.id
    assert again.conflict_type == "逻辑矛盾"
    assert again.user_state == "unseen"


def test_conflict_repo_user_scoped(session):
    """结构性越权防护：A 用户写一行，B 用户拿不到。"""
    repo_a = ConflictRepository(session, user_id="uA")
    row = repo_a.create(
        user_id="uA",
        item_a_id="i1",
        item_b_id="i2",
        claim_a_id="c1",
        claim_b_id="c2",
        conflict_type="视角分歧",
    )
    session.flush()

    repo_b = ConflictRepository(session, user_id="uB")
    import pytest

    with pytest.raises(PermissionError):
        repo_b.get(row.id)