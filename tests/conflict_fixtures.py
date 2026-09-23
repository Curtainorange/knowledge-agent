"""构造「知识条目 + 主张 + 未解冲突」的测试夹具。

视图模块、开场、简报与诊断、L4 相关性判断——四处都要同样的三层数据。
集中在这里一份：分散到各测试文件各写一遍的话，字段改一次要改四处，
而且很容易漏掉某处（project 复盘里「同一规则实现两遍必然分叉」的同一类问题）。
"""
from __future__ import annotations

from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository


def make_conflict(
    session,
    user_id: str,
    *,
    tag: str,
    statement_a: str,
    statement_b: str,
    conflict_type: str = "立场对立",
    confidence: float = 0.8,
    embedding_a: bytes | None = None,
    embedding_b: bytes | None = None,
):
    """建 2 条知识 + 各 1 条主张 + 1 条未处理冲突。

    返回 (conflict, item_a, item_b, claim_a, claim_b)。
    """
    krepo = KnowledgeRepository(session, user_id=user_id)
    crepo = ClaimRepository(session, user_id=user_id)

    item_a = krepo.create(user_id=user_id, title=f"{tag}·甲", content=f"{statement_a}（正文）")
    item_b = krepo.create(user_id=user_id, title=f"{tag}·乙", content=f"{statement_b}（正文）")
    claim_a = crepo.create(
        user_id=user_id, knowledge_item_id=item_a.id, statement=statement_a,
        topic=tag, embedding=embedding_a,
    )
    claim_b = crepo.create(
        user_id=user_id, knowledge_item_id=item_b.id, statement=statement_b,
        topic=tag, embedding=embedding_b,
    )
    conflict = ConflictRepository(session, user_id=user_id).create(
        user_id=user_id,
        item_a_id=item_a.id,
        item_b_id=item_b.id,
        claim_a_id=claim_a.id,
        claim_b_id=claim_b.id,
        conflict_type=conflict_type,
        confidence=confidence,
    )
    session.commit()
    return conflict, item_a, item_b, claim_a, claim_b
