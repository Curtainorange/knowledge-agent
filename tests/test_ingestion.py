"""知识接入测试：录入生成条目 + 计算 embedding（hash 后端）。"""
from __future__ import annotations

from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.ingestion.service import IngestionService
from app.retrieval.embedding import HashEmbedding


def test_add_knowledge_creates_and_embeds(session):
    svc = IngestionService(session, HashEmbedding())
    item = svc.add_knowledge(user_id="u1", title="数据库索引", content="B+树原理", tags=["db"])
    assert item.title == "数据库索引"
    assert item.source == "manual"
    assert item.embed_status == "embedded"
    assert item.embedding is not None
    assert item.read_progress == 0.0


def test_add_knowledge_visible_to_repo(session):
    svc = IngestionService(session, HashEmbedding())
    svc.add_knowledge(user_id="u1", title="番茄炒蛋", content="三个番茄两个蛋")
    repo = KnowledgeRepository(session, user_id="u1")
    items = repo.list_active("u1")
    assert len(items) == 1 and items[0].title == "番茄炒蛋"


def test_add_knowledge_user_scoped(session):
    svc = IngestionService(session, HashEmbedding())
    svc.add_knowledge(user_id="uA", title="甲的笔记", content="内容")
    svc.add_knowledge(user_id="uB", title="乙的笔记", content="内容")
    # 各用户只见自己的活动条目（list_active 强制 user_id 作用域）
    assert len(KnowledgeRepository(session, user_id="uA").list_active("uA")) == 1
    assert len(KnowledgeRepository(session, user_id="uB").list_active("uB")) == 1