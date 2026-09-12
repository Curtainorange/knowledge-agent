"""L1 认知挖掘测试：结构路由 located / clarify / 回合超限 / 空库。

通过 FakeProvider 注入给定 L1Route JSON，验证编排闭环与状态机；
真实 LLM（DeepSeek）只在运行时才被调用。
"""
from __future__ import annotations

import json

from app.agent.l1_orchestrator import L1Orchestrator
from app.domain.repositories.conversation_repository import ConversationRepository
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


def test_located_returns_snippet_and_embed_status(session):
    """定位后直接交付摘要，调用方无需二次请求即可展示。"""
    item = _ingest_one(session, title="数据库索引", content="B+树与哈希索引的区别，以及覆盖索引用法")
    orch = _orchestrator(session, [{"decision": "located", "item_ids": [item.id]}])
    result = orch.mine(user_id="u1", conversation_id=None, message="索引")

    located = result.located_items[0]
    assert located.snippet.startswith("B+树")
    assert "覆盖索引" in located.snippet
    assert located.embed_status == "embedded"


def test_read_hint_tracks_real_progress(session):
    """阅读提醒随真实进度变化；读完即静默——不再是恒真的死信号。"""
    item = _ingest_one(session, title="进度条目", content="一些内容")

    def _mine():
        orch = _orchestrator(session, [{"decision": "located", "item_ids": [item.id]}])
        return orch.mine(user_id="u1", conversation_id=None, message="线索")

    assert "还没开始读" in _mine().read_hint

    item.read_progress = 0.5
    session.flush()
    assert "已读 50%" in _mine().read_hint

    item.read_progress = 1.0
    session.flush()
    assert _mine().read_hint == ""


def test_candidates_are_exposed_with_detail(session):
    """候选要给出标题/摘要/得分/通道，用户才能判断模型是真定位到还是随手挑了一条。"""
    _ingest_one(session, title="数据库索引", content="B+树与哈希索引的适用场景")
    _ingest_one(session, title="番茄炒蛋", content="三个番茄两个蛋")

    orch = _orchestrator(session, [{"decision": "clarify", "question": "是哪一条？"}])
    result = orch.mine(user_id="u1", conversation_id=None, message="索引")

    assert result.state == "clarifying"
    assert result.candidates, "应返回候选明细"
    for candidate in result.candidates:
        assert candidate.item_id and candidate.title
        assert candidate.snippet
        assert candidate.score >= 0
    assert result.turn == 1
    assert result.max_turns == 3


def test_multiple_hits_are_all_delivered(session):
    """模型一次给出多个 item_id 时应全部交付，而不是只取第一条。"""
    first = _ingest_one(session, title="索引 A", content="B+树索引")
    second = _ingest_one(session, title="索引 B", content="哈希索引")

    orch = _orchestrator(session, [{"decision": "located", "item_ids": [first.id, second.id]}])
    result = orch.mine(user_id="u1", conversation_id=None, message="索引")

    assert result.state == "located"
    assert {item.item_id for item in result.located_items} == {first.id, second.id}


def test_located_dedupes_and_drops_unknown_ids(session):
    """重复 id 去重、不存在的 id 忽略——否则会把整个知识库重复倒给用户。"""
    item = _ingest_one(session)
    orch = _orchestrator(
        session,
        [{"decision": "located", "item_ids": [item.id, item.id, "not-a-real-id"]}],
    )
    result = orch.mine(user_id="u1", conversation_id=None, message="线索")

    assert result.state == "located"
    assert len(result.located_items) == 1
    assert result.located_items[0].item_id == item.id


def test_turn_count_is_not_polluted_by_other_messages(session):
    """同一会话里混入普通对话消息，不应把 L1 追问轮次顶到上限。

    旧实现统计会话内所有 assistant 消息，这些闲聊会把计数推到 max_turns，
    导致 L1 第一轮就跳过追问直接兜底。
    """
    _ingest_one(session)
    repo = ConversationRepository(session, user_id="u1")
    conversation = repo.create("u1")
    for i in range(3):
        repo.append_message(conversation, "user", f"闲聊 {i}")
        repo.append_message(conversation, "assistant", f"回复 {i}")  # 无 l1 标记
    session.flush()

    orch = _orchestrator(session, [{"decision": "clarify", "question": "再具体一点？"}])
    result = orch.mine(user_id="u1", conversation_id=conversation.id, message="索引")

    assert result.state == "clarifying", "不应被无关消息顶到上限而直接兜底"
    assert result.turn == 1


def test_unresolvable_ids_fall_back_to_clarify(session):
    """模型给出的 id 全部无效时，应降级为追问而不是返回空命中。"""
    _ingest_one(session)
    orch = _orchestrator(session, [{"decision": "located", "item_ids": ["ghost-1", "ghost-2"]}])
    result = orch.mine(user_id="u1", conversation_id=None, message="线索")

    assert result.state == "clarifying"
    assert result.located_items == []