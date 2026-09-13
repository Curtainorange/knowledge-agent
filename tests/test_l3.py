"""L3 认知助产测试（UC-L3-01 简报 / UC-L3-02 衔接追问）。

重点验证三件容易做错的事：
1. **计数由代码聚合**，不信模型给的数字（模型只做逐条归类）；
2. 模型幻觉出来的 item_id 要被丢弃，不能污染统计；
3. 解析失败要**降级而不崩**——主题分布拿到就返回主题分布，不被追问失败拖垮。
"""
from __future__ import annotations

import json

from app.agent.l3_orchestrator import L3Orchestrator
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from tests.helpers import auth_headers


class FakeProvider(LLMProvider):
    """按 task_type 回放；预案放字符串时直接作为原始文本返回（构造解析失败）。"""

    def __init__(self, rows_by_task: dict[str, list] | None = None) -> None:
        self.rows_by_task = {k: list(v) for k, v in (rows_by_task or {}).items()}
        self.calls: list[str] = []

    def chat(self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"):
        self.calls.append(task_type)
        rows = self.rows_by_task.get(task_type)
        if rows:
            row = rows.pop(0)
            text = row if isinstance(row, str) else json.dumps(row)
        else:
            text = json.dumps({"assignments": [], "patterns": [], "questions": [], "overview": ""})
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning)


def _orchestrator(session, rows) -> L3Orchestrator:
    return L3Orchestrator(ModelGateway(provider=FakeProvider(rows)), session)


def _seed(session, user_id: str, titles: list[str]) -> list[str]:
    repo = KnowledgeRepository(session, user_id=user_id)
    ids = []
    for title in titles:
        item = repo.create(user_id=user_id, title=title, content=f"{title} 的正文内容")
        ids.append(item.id)
    session.commit()
    return ids


# ---------- UC-L3-01 简报 ----------


def test_brief_empty_library(session):
    result = _orchestrator(session, {}).brief(user_id="u1")
    assert result.state == "empty"
    assert result.questions == []


def test_brief_aggregates_counts_locally_not_from_model(session):
    ids = _seed(session, "u1", ["如何开始健身", "如何开始写作", "健身平台期突破", "写作的结构化训练"])
    result = _orchestrator(session, {
        "topic_analysis": [{"assignments": [
            {"item_id": ids[0], "topic": "健身", "level": "入门"},
            {"item_id": ids[1], "topic": "写作", "level": "入门"},
            {"item_id": ids[2], "topic": "健身", "level": "进阶"},
            {"item_id": ids[3], "topic": "写作", "level": "实战"},
        ], "overview": "健身与写作两条线"}],
        "cognitive_brief": [{
            "patterns": ["大量存在：入门级内容", "完全缺失：平台期应对"],
            "questions": [{
                "question": "你在健身和写作上都停在入门层，真正的瓶颈是不是「入门后第 3-6 周」？",
                "why": "两条线都是入门 2 篇、进阶仅 1 篇",
                "evidence": "健身 2 篇（入门 1、进阶 1），写作 2 篇（入门 1、实战 1）",
                "next_step": "把平台期应对的碎片整理成一页手册",
            }],
            "overview": "两条线都浅",
        }],
    }).brief(user_id="u1")

    assert result.state == "ok"
    assert result.analyzed_items == 4
    # 计数来自本地聚合：健身 2 / 写作 2，且层级统计准确
    stats = {s.topic: s for s in result.topics}
    assert stats["健身"].count == 2
    assert stats["写作"].count == 2
    assert stats["健身"].levels == {"入门": 1, "进阶": 1}
    assert result.questions[0].evidence.startswith("健身 2 篇")
    assert len(result.questions) == 1


def test_brief_drops_hallucinated_item_ids(session):
    ids = _seed(session, "u1", ["条目甲", "条目乙"])
    result = _orchestrator(session, {
        "topic_analysis": [{"assignments": [
            {"item_id": ids[0], "topic": "甲组", "level": "入门"},
            {"item_id": "不存在的-id", "topic": "幻觉组", "level": "实战"},
        ]}],
        "cognitive_brief": [{"questions": [], "patterns": []}],
    }).brief(user_id="u1")

    topics = {s.topic: s.count for s in result.topics}
    assert topics == {"甲组": 1}  # 幻觉 id 被丢弃，不污染统计
    assert result.analyzed_items == 2


def test_brief_caps_question_count(session):
    """需求要求 2-3 个：模型多给了也要截断。"""
    ids = _seed(session, "u1", ["条目一", "条目二"])
    many = [{"question": f"问题{i}", "why": "w", "evidence": "e", "next_step": "n"} for i in range(6)]
    result = _orchestrator(session, {
        "topic_analysis": [{"assignments": [
            {"item_id": ids[0], "topic": "主题", "level": "入门"},
            {"item_id": ids[1], "topic": "主题", "level": "进阶"},
        ]}],
        "cognitive_brief": [{"questions": many, "patterns": ["大量存在：入门"]}],
    }).brief(user_id="u1")
    assert len(result.questions) == 3


def test_brief_degrades_when_classify_fails(session):
    _seed(session, "u1", ["条目甲"])
    result = _orchestrator(session, {"topic_analysis": ["这不是 JSON {{{"]}).brief(user_id="u1")
    assert result.state == "degraded"
    assert result.topics == []
    assert "主题归类失败" in result.note


def test_brief_keeps_topics_when_question_draft_fails(session):
    """追问失败不该把已经算好的主题分布一起丢掉——部分结果比没有结果有用。"""
    ids = _seed(session, "u1", ["条目甲"])
    result = _orchestrator(session, {
        "topic_analysis": [{"assignments": [{"item_id": ids[0], "topic": "主题甲", "level": "入门"}]}],
        "cognitive_brief": ["坏掉的 JSON {"],
    }).brief(user_id="u1")

    assert result.state == "degraded"
    assert [s.topic for s in result.topics] == ["主题甲"]
    assert "主题分布仍然有效" in result.note


def test_brief_reports_conflict_stats(session):
    ids = _seed(session, "u1", ["条目甲"])
    xrepo = ConflictRepository(session, user_id="u1")
    xrepo.create(
        user_id="u1", item_a_id="ia", item_b_id="ib",
        claim_a_id="ca", claim_b_id="cb", conflict_type="立场对立",
    )
    session.commit()

    result = _orchestrator(session, {
        "topic_analysis": [{"assignments": [{"item_id": ids[0], "topic": "主题", "level": "入门"}]}],
        "cognitive_brief": [{"questions": [], "patterns": [], "overview": ""}],
    }).brief(user_id="u1")

    assert result.conflict_stats["this_week"] == 1  # 刚落库的冲突算本周
    assert result.conflict_stats["by_state"].get("unseen") == 1


def test_brief_uses_off_reasoning_tasks(session):
    """分类与提问都属轻量任务：走 reasoning=off 的 task_type，别付思考溢价。"""
    ids = _seed(session, "u1", ["条目甲"])
    provider = FakeProvider({
        "topic_analysis": [{"assignments": [{"item_id": ids[0], "topic": "主题", "level": "入门"}]}],
        "cognitive_brief": [{"questions": [], "patterns": []}],
    })
    L3Orchestrator(ModelGateway(provider=provider), session).brief(user_id="u1")
    assert provider.calls == ["topic_analysis", "cognitive_brief"]


# ---------- UC-L3-02 衔接追问 ----------


def test_question_for_item_returns_single_question(session):
    ids = _seed(session, "u1", ["数据库索引实践", "查询优化笔记"])
    result = _orchestrator(session, {
        "cognitive_brief": [{
            "questions": [
                {"question": "索引设计以查询模式为先，那你的查询模式是怎么总结的？",
                 "why": "刚录入的条目谈索引", "evidence": "新条目正文 20 字", "next_step": "写下三条查询模式"},
                {"question": "第二个问题应当被截断", "why": "", "evidence": "", "next_step": ""},
            ],
            "patterns": [], "overview": "",
        }],
    }).question_for_item(user_id="u1", item_id=ids[0])

    assert result.state == "ok"
    assert len(result.questions) == 1
    assert "索引" in result.questions[0].question


def test_question_for_missing_item(session):
    result = _orchestrator(session, {}).question_for_item(user_id="u1", item_id="不存在")
    assert result.state == "empty"


def test_question_for_item_degrades_on_bad_json(session):
    ids = _seed(session, "u1", ["条目甲"])
    result = _orchestrator(session, {"cognitive_brief": ["坏 JSON {"]}).question_for_item(
        user_id="u1", item_id=ids[0]
    )
    assert result.state == "degraded"


# ---------- API ----------


def test_api_brief_and_question(client):
    from app.api import deps
    from app.main import app

    fake = FakeProvider({"topic_analysis": [], "cognitive_brief": []})
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=fake)
    try:
        headers = auth_headers(client, "l3_api_user")
        created = client.post(
            "/api/v1/knowledge/items",
            json={"title": "如何开始健身", "content": "第一步：选择适合自己的运动。"},
            headers=headers,
        ).json()

        # 归类预案需要用到真实 item_id，这里改用「回放时动态补 id」的方式：直接用空 assignments，
        # 验证接口契约与空库/降级路径的响应结构
        brief = client.post("/api/v1/l3/brief", headers=headers)
        assert brief.status_code == 200, brief.text
        body = brief.json()
        assert body["state"] in {"ok", "degraded", "empty"}
        assert "topics" in body and "questions" in body and body["request_id"]

        question = client.post(
            "/api/v1/l3/question", json={"item_id": created["item_id"]}, headers=headers
        )
        assert question.status_code == 200, question.text
        assert question.json()["state"] in {"ok", "degraded"}

        # 越权：B 用户拿不到 A 条目的追问
        headers_b = auth_headers(client, "l3_api_other")
        forbidden = client.post(
            "/api/v1/l3/question", json={"item_id": created["item_id"]}, headers=headers_b
        )
        assert forbidden.status_code == 403
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)


def test_api_l3_requires_auth(client):
    assert client.post("/api/v1/l3/brief").status_code == 401
    assert client.post("/api/v1/l3/question", json={"item_id": "x"}).status_code == 401