"""L1 认知挖掘测试：结构路由 located / clarify / 回合超限 / 空库。

通过 FakeProvider 注入给定 L1Route JSON，验证编排闭环与状态机；
真实 LLM（DeepSeek）只在运行时才被调用。
"""
from __future__ import annotations

import json

from app.agent.l1_orchestrator import L1Orchestrator
from app.ingestion.service import IngestionService
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.retrieval.embedding import HashEmbedding


class FakeProvider(LLMProvider):
    """按预设 JSON 顺序回放的结构化路由供应商。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = list(rows)

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        text = json.dumps(self.rows.pop(0)) if self.rows else json.dumps(
            {"decision": "clarify", "question": "吱？"}
        )
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


def _ingest_one(session, title="数据库索引", content="B+树与哈希索引的区别"):
    return IngestionService(session, HashEmbedding()).add_knowledge(
        user_id="u1", title=title, content=content
    )


def _orchestrator(session, rows, max_turns=3):
    return L1Orchestrator(ModelGateway(provider=FakeProvider(rows)), session, max_turns=max_turns)


def test_empty_knowledge_returns_empty(session):
    result = _orchestrator(session, []).mine(user_id="u1", conversation_id=None, message="模糊线索")
    assert result.state == "empty"
    assert "空" in result.question


def test_located_returns_item(session):
    item = _ingest_one(session)
    orch = _orchestrator(session, [{"decision": "located", "item_ids": [item.id]}])
    result = orch.mine(user_id="u1", conversation_id=None, message="那段讲索引的资料")
    assert result.state == "located"
    assert result.located_items[0].item_id == item.id
    assert result.located_items[0].read_progress == 0.0


def test_clarify_then_located(session):
    item = _ingest_one(session)
    rows = [
        {"decision": "clarify", "question": "你是想找数据库还是哈希？", "item_ids": []},
        {"decision": "located", "item_ids": [item.id]},
    ]
    orch = _orchestrator(session, rows)

    r1 = orch.mine(user_id="u1", conversation_id=None, message="关于索引的资料")
    assert r1.state == "clarifying"
    assert r1.question == "你是想找数据库还是哈希？"
    cid = r1.conversation_id

    r2 = orch.mine(user_id="u1", conversation_id=cid, message="数据库索引")
    assert r2.state == "located"
    assert r2.located_items[0].item_id == item.id
    assert r2.conversation_id == cid


def test_turn_cap_forces_fallback(session):
    _ingest_one(session, title="番茄炒蛋", content="三个番茄两个蛋")
    _ingest_one(session, title="数据库索引", content="B+树")
    # max_turns=1：第一轮 clarify 后，第二轮直接兜底定位首候选，不再调 LLM
    # （查询含可命中关键词，保证检索候选非空，从而进入超限兜底分支）
    orch = _orchestrator(session, [{"decision": "clarify", "question": "什么？"}], max_turns=1)
    r1 = orch.mine(user_id="u1", conversation_id=None, message="番茄")
    assert r1.state == "clarifying"
    r2 = orch.mine(user_id="u1", conversation_id=r1.conversation_id, message="番茄")
    assert r2.state == "located"
    assert r2.located_items  # 有兜底命中


def test_invalid_provider_json_falls_back_to_clarify(session):
    _ingest_one(session)

    class BrokenProvider(LLMProvider):
        def chat(self, **kwargs) -> Completion:
            return Completion(
                text="not json at all {", prompt_tokens=1, completion_tokens=1,
                finish_reason="stop", model="m", reasoning=False,
            )

    orch = L1Orchestrator(ModelGateway(provider=BrokenProvider()), session)
    result = orch.mine(user_id="u1", conversation_id=None, message="线索")
    assert result.state == "clarifying"
    assert result.question  # 回落通用问题