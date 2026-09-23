"""「L2 的产出真的流到了别处」——B 与 E 的验证（全 Mock，不触网）。

D（开场直击）在 tests/test_greeting.py 里验证，这里钉住另外两处：

- **B**：L3 简报与 L5 诊断拿到的**不只是冲突数量**，还有双方主张本身。
  只给计数，追问和归因就只能围绕一个数字展开，说不清矛盾在哪一点上。
- **E**：与学习目标语义相关的未解冲突会进入 L4 的计划上下文与归因；
  不相关的不进，判定不了（embedding 不可用）也不进——宁可少注入，
  也不能把无关矛盾塞进计划讨论，那会让用户以为计划卡在无关的事上。
"""
from __future__ import annotations

import json

from app.agent.l3_orchestrator import L3Orchestrator
from app.agent.l4_orchestrator import L4Orchestrator
from app.agent.l5_orchestrator import L5Orchestrator
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.retrieval.embedding import EmbeddingModel
from tests.conflict_fixtures import make_conflict

USER = "u1"


class CapturingProvider(LLMProvider):
    """按 task_type 回放，并记下每次调用收到的 messages（用于断言 prompt 内容）。"""

    def __init__(self, rows_by_task: dict[str, list] | None = None) -> None:
        self.rows_by_task = {k: list(v) for k, v in (rows_by_task or {}).items()}
        self.messages: dict[str, list] = {}

    def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
        self.messages.setdefault(task_type, []).append(messages)
        rows = self.rows_by_task.get(task_type)
        if rows:
            row = rows.pop(0)
            text = row if isinstance(row, str) else json.dumps(row)
        else:
            text = json.dumps({})
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning
        )

    def user_text(self, task_type: str) -> str:
        """该 task 首次调用的 user message（prompt 正文）。"""
        return self.messages[task_type][0][-1]["content"]


def _seed_item(session, title: str):
    item = KnowledgeRepository(session, user_id=USER).create(
        user_id=USER, title=title, content=f"{title} 的正文内容"
    )
    session.commit()
    return item


# ---- B：冲突内容进入 L3 简报与 L5 诊断 --------------------------------------


def test_brief_prompt_carries_conflict_content(session):
    """简报的追问原料里必须有冲突双方的主张，而不只是一个计数。"""
    item_a = _seed_item(session, "索引笔记")
    item_b = _seed_item(session, "数据库选型")
    make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )
    provider = CapturingProvider({
        "topic_analysis": [{"assignments": [
            {"item_id": item_a.id, "topic": "索引", "level": "入门"},
            {"item_id": item_b.id, "topic": "索引", "level": "入门"},
        ], "overview": "索引一条线"}],
        "cognitive_brief": [{"patterns": [], "questions": [], "overview": ""}],
    })

    result = L3Orchestrator(ModelGateway(provider=provider), session).brief(user_id=USER)

    assert result.state == "ok"
    text = provider.user_text("cognitive_brief")
    assert "B+树更适合范围查询" in text
    assert "哈希索引更适合范围查询" in text
    assert "尚未处理的观点冲突" in text


def test_brief_prompt_has_no_conflict_section_when_none(session):
    """没有未解冲突时不插入该段——空段落会诱导模型凭空提问。"""
    item = _seed_item(session, "索引笔记")
    provider = CapturingProvider({
        "topic_analysis": [{"assignments": [
            {"item_id": item.id, "topic": "索引", "level": "入门"},
        ], "overview": "x"}],
        "cognitive_brief": [{"patterns": [], "questions": [], "overview": ""}],
    })

    L3Orchestrator(ModelGateway(provider=provider), session).brief(user_id=USER)

    assert "尚未处理的观点冲突" not in provider.user_text("cognitive_brief")


def test_diagnosis_prompt_carries_conflict_content(session):
    """诊断归因要能看到冲突的具体内容，才能把「停滞」与「未解矛盾」联系起来。"""
    _seed_item(session, "索引笔记")
    make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )
    provider = CapturingProvider({"causal_reasoning": [{
        "pattern": "待观察", "root_cause": "样本还少", "suggested_action": "多记录几条",
        "confidence": 0.6, "reasoning_chain": [],
    }]})

    result = L5Orchestrator(ModelGateway(provider=provider), session).diagnose(user_id=USER)

    assert result.state == "ok"
    text = provider.user_text("causal_reasoning")
    assert "B+树更适合范围查询" in text
    assert "未处理冲突的具体内容" in text


# ---- E：未解冲突与学习目标的相关性 ------------------------------------------


class _FixedEmbedding:
    """把任何文本映射到同一个固定向量——把「语义相近」变成可控的测试条件。"""

    def __init__(self, vec) -> None:
        self._vec = [float(x) for x in vec]
        self.dim = len(self._vec)

    def embed(self, texts):
        return [list(self._vec) for _ in texts]


class _BoomEmbedding:
    """embed 直接抛错（模拟向量模型没装好 / 加载失败）。"""

    dim = 3

    def embed(self, texts):
        raise RuntimeError("embedding unavailable")


def test_relevant_conflict_enters_plan_context(session):
    """目标向量与主张向量同向（余弦 1.0）→ 视为相关，注入计划上下文。"""
    vec = [1.0, 0.0, 0.0]
    make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
        embedding_a=EmbeddingModel.dumps(vec),
        embedding_b=EmbeddingModel.dumps(vec),
    )
    orch = L4Orchestrator(session=session, embedding=_FixedEmbedding(vec))

    context = orch._knowledge_context(USER, "掌握数据库索引优化")

    assert "与该目标相关的未解观点冲突" in context
    assert "B+树更适合范围查询" in context


def test_unrelated_conflict_stays_out_of_plan_context(session):
    """目标向量与主张向量正交（余弦 0）→ 不相关，不注入。"""
    make_conflict(
        session, USER, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
        embedding_a=EmbeddingModel.dumps([1.0, 0.0, 0.0]),
        embedding_b=EmbeddingModel.dumps([1.0, 0.0, 0.0]),
    )
    orch = L4Orchestrator(session=session, embedding=_FixedEmbedding([0.0, 1.0, 0.0]))

    assert orch._relevant_conflicts(user_id=USER, goal_description="掌握索引优化") == []
    assert "未解观点冲突" not in orch._knowledge_context(USER, "掌握索引优化")


def test_conflicts_skipped_when_embedding_unavailable(session):
    """向量不可用时不注入（而不是把所有冲突都当成相关）。"""
    make_conflict(
        session, USER, tag="索引", statement_a="A", statement_b="B",
        embedding_a=EmbeddingModel.dumps([1.0, 0.0, 0.0]),
    )
    orch = L4Orchestrator(session=session, embedding=_BoomEmbedding())

    assert orch._relevant_conflicts(user_id=USER, goal_description="索引优化") == []


def test_conflicts_skipped_without_goal(session):
    """没有目标就谈不上「与目标相关」，直接不注入。"""
    make_conflict(
        session, USER, tag="索引", statement_a="A", statement_b="B",
        embedding_a=EmbeddingModel.dumps([1.0, 0.0, 0.0]),
    )
    orch = L4Orchestrator(session=session, embedding=_FixedEmbedding([1.0, 0.0, 0.0]))

    assert orch._relevant_conflicts(user_id=USER, goal_description="   ") == []


def test_conflict_without_stored_vector_does_not_crash(session):
    """主张没有向量（未向量化）时跳过该条，不抛错。"""
    make_conflict(session, USER, tag="索引", statement_a="A", statement_b="B")
    orch = L4Orchestrator(session=session, embedding=_FixedEmbedding([1.0, 0.0, 0.0]))

    assert orch._relevant_conflicts(user_id=USER, goal_description="索引优化") == []
