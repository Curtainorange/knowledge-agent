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
from app.domain.models.book import Book
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


# ---- L3 认知简报 -----------------------------------------------------------

BRIEF_TEXT = "生成认知简报"


def _topic_payload(item_id: str, topic: str = "数据库", level: str = "进阶") -> str:
    return json.dumps({
        "assignments": [{"item_id": item_id, "topic": topic, "level": level}],
        "overview": "整体偏入门",
    })


def _draft_payload() -> str:
    return json.dumps({
        "patterns": ["大量存在：入门层内容", "完全缺失：实战层内容"],
        "questions": [{
            "question": "你打算怎么把索引原理用到真实慢查询上？",
            "why": "只有原理没有落地",
            "evidence": "1 篇里 0 篇是实战层",
            "next_step": "挑一条线上慢查询做一次执行计划分析",
        }],
        "overview": "结构上有明显缺口",
    })


def test_l3_brief_returns_full_card(client, session, provider):
    headers = auth_headers(client, "agent_l3_ok")
    item_id = _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.extend([_topic_payload(item_id), _draft_payload()])

    body = _send(client, headers, BRIEF_TEXT)

    assert body["capability"] == "l3"
    assert body["decided_by"] == "local"          # 命令式说法，不花分流调用
    card = body["card"]
    assert card["kind"] == "l3_brief"
    assert card["state"] == "ok"
    assert card["topics"] == [{"topic": "数据库", "count": 1, "levels": {"进阶": 1}}]
    assert card["questions"][0]["evidence"] == "1 篇里 0 篇是实战层"
    assert card["href"] == "/brief.html"          # 卡片上留一条回原页面的深链
    assert "值得想的问题" in body["reply"]


def test_l3_brief_on_empty_knowledge_skips_the_model(client, provider):
    """空知识库直接返回提示，不该为了「生成简报」白花两次模型调用。"""
    headers = auth_headers(client, "agent_l3_empty")
    body = _send(client, headers, BRIEF_TEXT)

    assert body["card"]["state"] == "empty"
    assert provider.task_types == []


def test_l3_degraded_keeps_the_usable_part(client, session, provider):
    """主题归类成功、追问生成失败 → 保留主题分布，并把降级原因说清楚。

    这里最容易做错的是「一句失败就把整张卡丢掉」——实际上分布表仍然有效，
    用户看得到东西；而且在回复文案里也不能说「失败」（那会让人直接不看了）。
    """
    headers = auth_headers(client, "agent_l3_degraded")
    item_id = _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.extend([_topic_payload(item_id), "追问那一步我没输出 JSON"])

    body = _send(client, headers, BRIEF_TEXT)

    assert body["card"]["state"] == "degraded"
    assert body["card"]["topics"], "降级时主题分布必须保留"
    assert body["card"]["note"]
    # 锁的是「必须说明还有哪部分可用」，而不是「不许出现失败二字」：
    # 只丢一句「生成失败」会让用户直接不看，而分布表其实还是好的
    assert "主题分布仍然有效" in body["reply"]


def test_l3_writes_exactly_one_exchange(client, session, provider):
    """L3 自己什么都不写，两条消息都要本层补——漏一条历史就断片。"""
    headers = auth_headers(client, "agent_l3_hist")
    item_id = _seed_item(client, headers, "数据库索引", "B+树与哈希索引的适用场景")
    provider.rows.extend([_topic_payload(item_id), _draft_payload()])

    body = _send(client, headers, BRIEF_TEXT)
    messages = _messages(session, body["conversation_id"])

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["card"]["kind"] == "l3_brief"
    assert messages[-1]["source"] == "agent"


# ---- 未接入的能力 -----------------------------------------------------------


def test_unwired_capability_returns_guide_card_without_side_effects(client, session, provider):
    """认得意图 ≠ 已经接入。回引导卡可以，偷偷跑一遍那个能力不行。

    用「书架」这条还没接进来的路径来锁：路由得认出来（否则会兜底成闲聊），
    但必须只回引导卡，且一条书都不该被写进来。
    """
    headers = auth_headers(client, "agent_guide")
    body = _send(client, headers, "我的书架里有什么")

    assert body["capability"] == "books"
    assert body["card"]["kind"] == "guide"
    assert body["card"]["href"] == "/books.html"
    assert "书架" in body["reply"]
    assert provider.task_types == []              # 引导卡不该顺手调模型
    session.flush()
    assert session.query(Book).count() == 0       # 没有任何副作用


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
    wired = {name for name, item in items.items() if item["wired"]}
    assert wired == {
        "l1", "l2", "l3", "l4_goal", "l4_plan", "l4_deviation", "l5",
        "knowledge_add", "weread_sync",
    }
    # 只剩书架还没接进对话
    assert items["books"]["wired"] is False
    assert items["books"]["href"] == "/books.html"
    # 未接入的能力必须给得出原页面地址，否则引导卡是个死胡同
    for item in items.values():
        assert item["label"] and item["example"]
        if not item["wired"]:
            assert item["href"]
