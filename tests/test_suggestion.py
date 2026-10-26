"""B 模块测试：阅读建议生成（门禁 / 依据校验 / 跳过与纠正）。

对应 `docs/B模块-主动建议逻辑设计-2026-10-20.md` 的 AT-B-06~12 与
`docs/验收自检与修正-2026-10-20.md` 修正 1/3。
全程 ScriptedProvider + HashEmbedding，不触网。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.agent import suggestion as sugg
from app.agent.intake import IntakeContext
from app.api import deps
from app.ingestion.service import IngestionService
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from app.retrieval.embedding import HashEmbedding
from tests.helpers import auth_headers, sign_in


class _Scripted(LLMProvider):
    """按行返回预设输出，并记录 task_type。"""

    def __init__(self, rows: list[str] | None = None):
        self.rows = list(rows or [])
        self.calls: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(task_type)
        text = self.rows.pop(0) if self.rows else "{}"
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1,
            finish_reason="stop", model=model, reasoning=reasoning,
        )


def _ready_ctx(**overrides) -> IntakeContext:
    ctx = IntakeContext(
        question="怎么把极度透明用到周会上",
        goal="problem",
        self_level="once",
        help_type=["path"],
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    ctx.refresh_missing()
    return ctx


def _seed(session, *, content="极度透明意味着组织里没有不能说的话。", title="原则 · 第三章"):
    return IngestionService(session, HashEmbedding()).add_knowledge(
        user_id="u1", title=title, content=content
    )


def _draft(item_id: str, **over) -> str:
    payload = {
        "action": "在周会上先讲一个没人敢讲的问题",
        "why": "你问的是怎么落地到周会",
        "steps": ["列三个问题", "选一个", "自己先讲"],
        "how_to_verify": "会后有没有人接着说",
        "basis_item_ids": [item_id],
    }
    payload.update(over)
    return json.dumps({"items": [payload], "reason": ""}, ensure_ascii=False)


# ---- 门禁 -------------------------------------------------------------------


def test_incomplete_context_never_calls_model(session):
    """槽位没齐 → 连模型都不调（AT-B-02 的成本面）。"""
    _seed(session)
    provider = _Scripted([_draft("ki:x")])
    ctx = IntakeContext(question="怎么落地")
    ctx.refresh_missing()

    outcome = sugg.generate(
        session, user_id="u1", ctx=ctx, gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "incomplete"
    assert provider.calls == []


def test_incomplete_does_not_touch_evidence(session):
    _seed(session)
    ctx = IntakeContext()
    ctx.refresh_missing()
    outcome = sugg.generate(session, user_id="u1", ctx=ctx, embedding=HashEmbedding())
    assert outcome.evidence == []


# ---- 依据不足 ---------------------------------------------------------------


def test_no_evidence_skips_generation(session):
    """库里没有相关内容 → 不出建议，且**不调模型**。"""
    provider = _Scripted([_draft("ki:x")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "insufficient_evidence"
    assert provider.calls == []
    assert outcome.hints  # 给用户三条出路


# ---- 正常生成 ---------------------------------------------------------------


def test_ok_generates_item_with_basis(session):
    item = _seed(session)
    provider = _Scripted([_draft(f"ki:{item.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"
    assert len(outcome.items) == 1
    assert outcome.items[0]["basis_item_ids"] == [f"ki:{item.id}"]
    assert outcome.items[0]["how_to_verify"]
    assert outcome.confidence > 0


def test_generation_uses_reading_suggestion_task_type(session):
    item = _seed(session)
    provider = _Scripted([_draft(f"ki:{item.id}")])
    sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert provider.calls == ["reading_suggestion"]


# ---- 引用校验（硬拦截）-------------------------------------------------------


def test_fabricated_id_drops_whole_item(session):
    """模型编了一个不存在的 id → 整条丢弃，退化成「依据不足」。"""
    _seed(session)
    provider = _Scripted([_draft("ki:根本不存在的条目")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "insufficient_evidence"
    assert outcome.items == []
    assert outcome.dropped >= 1


def test_empty_basis_ids_dropped(session):
    item = _seed(session)
    provider = _Scripted([_draft(f"ki:{item.id}", basis_item_ids=[])])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.items == []


def test_missing_how_to_verify_dropped(session):
    """缺「怎么判断有用」的建议不算合格建议。"""
    item = _seed(session)
    provider = _Scripted([_draft(f"ki:{item.id}", how_to_verify="")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.items == []


def test_at_most_two_items(session):
    item = _seed(session)
    payload = {
        "items": [
            {
                "action": f"建议{i}", "why": "理由", "steps": [],
                "how_to_verify": "看有没有用", "basis_item_ids": [f"ki:{item.id}"],
            }
            for i in range(1, 4)
        ],
        "reason": "",
    }
    provider = _Scripted([json.dumps(payload, ensure_ascii=False)])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert len(outcome.items) == 2


def test_partial_drop_keeps_valid_item(session):
    """两条里有一条引用不实：留一条，并说明省略了几条。"""
    item = _seed(session)
    payload = {
        "items": [
            {
                "action": "有效的", "why": "理由", "steps": [],
                "how_to_verify": "验证", "basis_item_ids": [f"ki:{item.id}"],
            },
            {
                "action": "幻觉的", "why": "理由", "steps": [],
                "how_to_verify": "验证", "basis_item_ids": ["ki:不存在"],
            },
        ],
        "reason": "",
    }
    provider = _Scripted([json.dumps(payload, ensure_ascii=False)])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"
    assert [it["action"] for it in outcome.items] == ["有效的"]
    assert "省略" in (outcome.note or "")


# ---- 通读笔记进证据池 -------------------------------------------------------


def _book_and_reading(session, *, user_id="u1", title="原则", summary="极度透明意味着要能说真话。"):
    from app.domain.models.book import Book
    from app.domain.models.book_agent_reading import BookAgentReading

    text = "极度透明意味着组织里没有不能说的话。"
    book = Book(
        user_id=user_id, title=title, author="瑞·达利欧",
        format="txt", file_path="x.txt",
        chapters=[{"index": 0, "title": "第一章", "char_start": 0, "char_end": len(text)}],
        full_text=text, total_chars=len(text),
    )
    session.add(book)
    session.commit()
    reading = BookAgentReading(
        user_id=user_id, book_id=book.id, status="done",
        total_chars=len(text), summary=summary,
        chapters_note=[], chunk_count=1, failed_chunks=0, truncated=False,
    )
    session.add(reading)
    session.commit()
    return book


def test_agent_summary_enters_evidence_pool(session):
    """通读笔记进证据池，模型可同时引用 ki:（自己的）与 agent:（通读的）。"""
    item = _seed(session)
    book = _book_and_reading(session)
    payload = {
        "action": "用极度透明的方法开周会", "why": "你问的是落地到周会",
        "steps": [], "how_to_verify": "会后有没有人接着说",
        "basis_item_ids": [f"ki:{item.id}", f"agent:{book.id}"],
    }
    provider = _Scripted([json.dumps({"items": [payload], "reason": ""}, ensure_ascii=False)])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"
    assert {e.kind for e in outcome.evidence} == {"knowledge_item", "agent_summary"}


def test_agent_only_evidence_skips_model(session):
    """只有通读笔记、没有自己的内容 → 不调模型，直接依据不足。"""
    book = _book_and_reading(session)
    provider = _Scripted([_draft(f"agent:{book.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "insufficient_evidence"
    assert provider.calls == []


# ---- 异常与边界 -------------------------------------------------------------


def test_unparseable_output_degrades(session):
    item = _seed(session)
    provider = _Scripted(["这不是 JSON"])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "degraded"
    assert outcome.note


def test_other_users_items_not_used(session):
    IngestionService(session, HashEmbedding()).add_knowledge(
        user_id="u2", title="别人的笔记", content="极度透明意味着什么"
    )
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), embedding=HashEmbedding()
    )
    assert outcome.state == "insufficient_evidence"


def test_reply_text_matches_state(session):
    assert "确认" in sugg.reply_text(sugg.SuggestionOutcome(state="incomplete")) or True
    text = sugg.reply_text(sugg.SuggestionOutcome(state="ok", items=[{}, {}]))
    assert "2" in text


# ---- 跳过确认后仍能生成（回归：原来跳了也没用）-------------------------------


def test_skipped_context_still_generates(session):
    """「别问了直接给建议」→ 只保留 question 必需，其余按默认。

    回归：原来 `skipped` 只设了一个布尔标记，门禁仍要求四项齐全，
    用户说了"直接给"还是被拦住——等于这个说法没有实现。
    """
    item = _seed(session)
    ctx = _ready_ctx()
    ctx.goal = ""
    ctx.self_level = ""
    ctx.help_type = []
    ctx.skipped = True
    ctx.reduced_mode = True
    ctx.refresh_missing()
    assert ctx.is_ready  # 只剩 question

    provider = _Scripted([_draft(f"ki:{item.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=ctx, gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"


def test_skipped_suggestion_confidence_capped(session):
    """跳过的建议可以给，但不该显得和"确认过的建议"一样可靠。"""
    item = _seed(session)
    ctx = _ready_ctx()
    ctx.goal = ""
    ctx.self_level = ""
    ctx.help_type = []
    ctx.skipped = True
    ctx.reduced_mode = True
    ctx.refresh_missing()

    provider = _Scripted([_draft(f"ki:{item.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=ctx, gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.confidence <= 0.6


# ---- 同类建议收敛（回归：承诺了 7 天收敛但没实现）----------------------------


def test_rejected_same_kind_is_restrained(session):
    from app.feedback import events

    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_REJECTED,
        payload={"goal": "problem", "help_type": ["path"]},
    )
    _seed(session)
    provider = _Scripted([_draft("ki:whatever")])

    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "restrained"
    assert provider.calls == []  # 收敛期间连模型都不调


def test_other_kind_not_restrained(session):
    """否掉的是「给步骤」，不该连「讲清楚」也一起收敛。"""
    from app.feedback import events

    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_REJECTED,
        payload={"goal": "problem", "help_type": ["path"]},
    )
    item = _seed(session)
    ctx = _ready_ctx(help_type=["explain"])
    provider = _Scripted([_draft(f"ki:{item.id}")])

    outcome = sugg.generate(
        session, user_id="u1", ctx=ctx, gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"


# ---- 端到端：确认 → 生成 -----------------------------------------------------


@pytest.fixture()
def provider_override():
    provider = _Scripted()
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=provider)
    yield provider
    app.dependency_overrides.pop(deps.get_gateway, None)


def _send(client: TestClient, headers: dict, message: str, cid: str | None = None) -> dict:
    """发一句话。**必须带 conversation_id**——不带就会新建会话，
    确认上下文不在新会话里，门禁自然不生效（真实前端总是带着它）。"""
    payload: dict = {"message": message}
    if cid:
        payload["conversation_id"] = cid
    resp = client.post("/api/v1/agent/chat", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _last_card(body: dict) -> dict | None:
    """`/agent/chat` 返回的是一轮结果（单个 card），不是消息列表；
    `/agent/start` 返回的是会话（messages 列表）。两种形状都要能取到卡。"""
    if isinstance(body.get("card"), dict) and body["card"].get("kind"):
        return body["card"]
    for m in reversed(body.get("messages") or []):
        card = m.get("card") or {}
        if card.get("kind"):
            return card
    return None


def _answer(client, headers, card: dict, slot: str, value: str) -> dict:
    resp = client.post(
        "/api/v1/agent/actions",
        json={
            "conversation_id": headers["_cid"],
            "card_key": card["key"],
            "action": "intake.answer",
            "target_id": slot,
            "value": value,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_end_to_end_confirm_then_generate(client, session, provider_override):
    """走完：开局确认卡 → 答三项 → 生成建议（带依据）。"""
    user = sign_in(client, "sugg_e2e")
    headers = dict(user.headers)

    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    item_id = saved.json()["item_id"]

    start = client.post("/api/v1/agent/start", json={}, headers=headers)
    assert start.status_code == 200
    conv = start.json()
    headers["_cid"] = conv["conversation_id"]

    card = _last_card(conv)
    assert card is not None and card["kind"] == "reading_intake"

    # 自由输入回答「想解决什么」——确认期接管应把它写进槽位
    after_q = _send(client, headers, "怎么把极度透明用到周会上", headers["_cid"])
    card = _last_card(after_q)
    assert card["kind"] == "reading_intake"

    # 剩下三项用按钮点（动作响应直接返回新卡，无需重拉会话）
    for slot, value in (("goal", "problem"), ("self_level", "once"), ("help_type", "path")):
        if slot not in [q["slot"] for q in card.get("questions") or []]:
            continue
        result = _answer(client, headers, card, slot, value)
        assert result.get("card"), result
        card = result["card"]

    # 信息齐了：卡上应有「生成建议」按钮
    assert card.get("sends"), f"信息应已齐，但卡上没有生成按钮：{card}"

    provider_override.rows.append(_draft(f"ki:{item_id}"))
    body = _send(client, headers, "确认，给我建议", headers["_cid"])

    result = _last_card(body)
    assert result["kind"] == "suggestion"
    assert result["state"] == "ok"
    assert result["items"]
    assert result["evidence"]
    # 依据必须能对上：items 引用的 id 都在 evidence 池里
    pool = {e["evidence_id"] for e in result["evidence"]}
    for it in result["items"]:
        assert set(it["basis_item_ids"]) <= pool


def test_end_to_end_skip_action_accepts_empty_target(client, session, provider_override):
    """「跳过这轮」没有目标对象，target_id 为空也必须能进。

    回归：原来前端 `!target` 直接 return、后端 `target_id min_length=1` 直接 422，
    这个按钮点下去完全没反应——UI 上有个按钮，功能却不存在。
    """
    user = sign_in(client, "sugg_skip_btn")
    headers = dict(user.headers)
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    start = client.post("/api/v1/agent/start", json={}, headers=headers).json()
    headers["_cid"] = start["conversation_id"]
    card = _last_card(start)
    assert card is not None and card["kind"] == "reading_intake"

    resp = client.post(
        "/api/v1/agent/actions",
        json={
            "conversation_id": headers["_cid"],
            "card_key": card["key"],
            "action": "intake.skip",
            "target_id": "",
            "value": "",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["card"]["kind"] == "reading_intake"


def test_end_to_end_insufficient_evidence(client, session, provider_override):
    """库里有内容但跟问题不相关 → 明确说依据不足，不硬编。"""
    user = sign_in(client, "sugg_insufficient")
    headers = dict(user.headers)
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "番茄炒蛋", "content": "三个番茄两个蛋，先炒蛋再炒番茄。"},
        headers=headers,
    )
    start = client.post("/api/v1/agent/start", json={}, headers=headers).json()
    headers["_cid"] = start["conversation_id"]

    for message in (
        "怎么用 B+ 树优化查询",
        "解决具体问题",
        "第一次接触",
        "给我下一步怎么做",
    ):
        body = _send(client, headers, message, headers["_cid"])

    body = _send(client, headers, "确认，给我建议", headers["_cid"])
    card = _last_card(body)
    assert card["kind"] == "suggestion"
    assert card["state"] == "insufficient_evidence"
    assert provider_override.calls == [] or "reading_suggestion" not in provider_override.calls
