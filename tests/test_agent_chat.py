"""对话入口端到端测试（全 Mock，不触网）。

覆盖三件在「对话即入口」里最容易悄悄做错的事：

1. **谁写了消息**。各能力自己追加消息（Orchestrator 写两条、L1 写追问），
   本层只补它没写的那条。写重了用户会看到两条同样的提问，写漏了历史就断片。
2. **未接入的能力不能假装做完**。路由认得 L2~L5，但这一轮只接了 L1 与知识录入，
   其余必须回一张「去哪儿」的引导卡，且**不能产生任何副作用**。
3. **有副作用的入口要能拒绝**。「记一下」但没说记什么时，不能落一条空条目。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.domain.models.conflict import Conflict
from app.domain.models.conversation import Conversation
from app.domain.models.knowledge_item import KnowledgeItem
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.helpers import auth_headers

NOTE_TEXT = "记一下：B+树更适合范围查询 #数据库"
LOCATE_TEXT = "之前存的那段讲查询优化的内容"


class ScriptedProvider(LLMProvider):
    """按脚本回放的供应商：`rows` 用完则回一段非 JSON（触发分流兜底路径）。"""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.task_types: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.task_types.append(task_type)
        text = self.rows.pop(0) if self.rows else "（脚本已用尽）"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


@pytest.fixture()
def provider():
    """把网关换成脚本供应商。注意依赖覆盖只作用于本次用例。"""
    scripted = ScriptedProvider()

    def override_gateway():
        return ModelGateway(provider=scripted)

    app.dependency_overrides[deps.get_gateway] = override_gateway
    yield scripted
    app.dependency_overrides.pop(deps.get_gateway, None)


def _send(client: TestClient, headers: dict, message: str, conversation_id: str | None = None) -> dict:
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed_item(client: TestClient, headers: dict, title: str, content: str) -> str:
    resp = client.post(
        "/api/v1/knowledge/items", json={"title": title, "content": content}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["item_id"]


def _messages(session, conversation_id: str) -> list[dict]:
    conv = session.get(Conversation, conversation_id)
    assert conv is not None
    return list(conv.messages or [])


# ---- 知识录入 ---------------------------------------------------------------


def test_note_creates_item_and_card(client, session, provider):
    headers = auth_headers(client, "agent_note")
    body = _send(client, headers, NOTE_TEXT)

    assert body["capability"] == "knowledge_add"
    assert body["decided_by"] == "local"          # 命令式说法不该花钱问模型
    assert provider.task_types == []
    assert body["card"]["kind"] == "knowledge_created"
    assert body["card"]["title"] == "B+树更适合范围查询"
    assert body["card"]["tags"] == ["数据库"]

    session.flush()
    items = session.query(KnowledgeItem).filter(KnowledgeItem.title == "B+树更适合范围查询").all()
    assert len(items) == 1
    assert items[0].source == "agent"             # 与表单录入（manual）区分得开


def test_note_without_body_writes_nothing(client, session, provider):
    """「记一下」但没说记什么 → 明确拒绝，绝不留一条空条目。"""
    headers = auth_headers(client, "agent_note_empty")
    body = _send(client, headers, "记一下")

    assert body["card"]["kind"] == "note_empty"
    session.flush()
    assert session.query(KnowledgeItem).count() == 0


def test_note_reply_and_history_are_consistent(client, session, provider):
    """用户消息只写一条，助手消息只写一条——重复写会让历史里冒出两条同样的回复。"""
    headers = auth_headers(client, "agent_note_hist")
    body = _send(client, headers, NOTE_TEXT)
    messages = _messages(session, body["conversation_id"])

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == NOTE_TEXT
    assert messages[1]["card"]["kind"] == "knowledge_created"


# ---- L1 挖掘 ---------------------------------------------------------------


def test_l1_located_returns_card_with_link(client, session, provider):
    headers = auth_headers(client, "agent_l1_hit")
    item_id = _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.append(json.dumps({"decision": "located", "item_ids": [item_id]}))

    body = _send(client, headers, LOCATE_TEXT)

    assert body["capability"] == "l1"
    card = body["card"]
    assert card["kind"] == "l1_located"
    assert card["items"][0]["item_id"] == item_id
    assert card["items"][0]["href"] == f"/knowledge.html?item={item_id}"


def test_l1_clarify_question_is_written_once(client, session, provider):
    """追问由 L1 自己落库；本层再写一遍，用户会看到两条一模一样的提问。"""
    headers = auth_headers(client, "agent_l1_clarify")
    _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.append(json.dumps({"decision": "clarify", "question": "是数据库还是哈希？"}))

    body = _send(client, headers, LOCATE_TEXT)

    assert body["card"]["kind"] == "l1_clarify"
    assert body["state"] == "clarifying"
    assistants = [m for m in _messages(session, body["conversation_id"]) if m["role"] == "assistant"]
    assert len(assistants) == 1
    assert assistants[0]["content"] == "是数据库还是哈希？"


def test_l1_empty_knowledge_is_reported(client, provider):
    headers = auth_headers(client, "agent_l1_empty")
    body = _send(client, headers, LOCATE_TEXT)

    assert body["card"]["kind"] == "l1_empty"
    assert provider.task_types == []              # 空库不必浪费一次模型调用


def test_l1_located_card_is_persisted_for_history(client, session, provider):
    """结论要留在历史里：刷新页面后重新渲染的是卡片，不是一句干巴巴的文本。"""
    headers = auth_headers(client, "agent_l1_hist")
    item_id = _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.append(json.dumps({"decision": "located", "item_ids": [item_id]}))

    body = _send(client, headers, LOCATE_TEXT)
    last = _messages(session, body["conversation_id"])[-1]

    assert last["role"] == "assistant"
    assert last["source"] == "agent"              # 与 L1 自己的追问（source=l1）区分
    assert last["card"]["kind"] == "l1_located"


# ---- 未接入的能力 -----------------------------------------------------------


def test_unwired_capability_returns_guide_card_without_side_effects(client, session, provider):
    """认得意图 ≠ 已经接入。回引导卡可以，偷偷跑一遍 L2 不行。"""
    headers = auth_headers(client, "agent_guide")
    _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")

    body = _send(client, headers, "扫描知识冲突")

    assert body["capability"] == "l2"
    assert body["card"]["kind"] == "guide"
    assert body["card"]["href"] == "/conflicts.html"
    assert "冲突检测" in body["reply"]
    session.flush()
    assert session.query(Conflict).count() == 0   # 没有任何副作用


# ---- 通用对话 ---------------------------------------------------------------


def test_plain_chat_has_no_card(client, provider):
    """闲聊/提问走通用对话：不带卡，也不能被硬塞进某个能力。"""
    headers = auth_headers(client, "agent_chat")
    # 先分流（模型判定为 chat），再走一轮通用对话
    provider.rows.append(
        json.dumps({"capability": "chat", "args": {}, "confidence": 0.9, "reason": "只是闲聊"})
    )
    provider.rows.append("这是一句闲聊回复")

    body = _send(client, headers, "今天天气不错")

    assert body["capability"] == "chat"
    assert body["decided_by"] == "model"
    assert body["card"] is None
    assert body["reply"] == "这是一句闲聊回复"
    assert provider.task_types == ["capability_routing", "multi_turn_dialogue"]


def test_unclassifiable_text_falls_back_to_chat(client, provider):
    """分流模型输出不可解析时回落通用对话——这是设计好的安全方向。"""
    headers = auth_headers(client, "agent_fallback")
    provider.rows.append("我觉得你想找东西，但我不输出 JSON")

    body = _send(client, headers, "嗯……那个")

    assert body["capability"] == "chat"
    assert body["decided_by"] == "fallback"
    assert provider.task_types == ["capability_routing", "multi_turn_dialogue"]


# ---- 会话与鉴权 -------------------------------------------------------------


def test_conversation_is_reused_across_capabilities(client, session, provider):
    """一条长线程：换能力不换会话，历史自然连在一起。"""
    headers = auth_headers(client, "agent_thread")
    first = _send(client, headers, NOTE_TEXT)
    second = _send(client, headers, "今天天气不错", first["conversation_id"])

    assert second["conversation_id"] == first["conversation_id"]
    roles = [m["role"] for m in _messages(session, first["conversation_id"])]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_foreign_conversation_id_starts_a_new_one(client, session, provider):
    """拿到别人的（或过期的）会话 id 时开新会话，而不是抛 500 或读别人的历史。"""
    owner = auth_headers(client, "agent_owner")
    owner_conv = _send(client, owner, NOTE_TEXT)["conversation_id"]

    intruder = auth_headers(client, "agent_intruder")
    body = _send(client, intruder, "今天天气不错", owner_conv)

    assert body["conversation_id"] != owner_conv
    assert _messages(session, body["conversation_id"])[0]["content"] == "今天天气不错"


def test_agent_chat_requires_auth(client):
    assert client.post("/api/v1/agent/chat", json={"message": "你好"}).status_code == 401


def test_request_id_is_returned(client, provider):
    headers = auth_headers(client, "agent_reqid")
    body = _send(client, headers, "今天天气不错")
    assert body["request_id"]


# ---- 能力目录 ---------------------------------------------------------------


def test_capabilities_endpoint_lists_wired_flags(client):
    resp = client.get("/api/v1/agent/capabilities")
    assert resp.status_code == 200

    items = {item["capability"]: item for item in resp.json()}
    assert items["l1"]["wired"] is True
    assert items["knowledge_add"]["wired"] is True
    assert items["l2"]["wired"] is False
    assert items["l2"]["href"] == "/conflicts.html"
    for item in items.values():
        assert item["label"] and item["example"]
