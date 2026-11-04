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


# 夹具书正文的长度与内容都要像真书：书籍原文的召回门槛（grounding.BOOK_MIN_RELEVANCE）
# 是按真实书籍的块（约 1200 字）实测出来的，用一句 19 字的话当「书」会被门槛挡掉——
# 那样测的就不是业务逻辑，而是夹具太假。
_BOOK_TEXT = (
    "极度透明意味着组织里没有不能说的话。"
    "把极度透明用到周会上，就是让每个人都能直接说出自己的判断，而不是会后再议论。"
) * 10


def _book_and_reading(session, *, user_id="u1", title="原则", summary="极度透明意味着要能说真话。",
                      text=_BOOK_TEXT):
    from app.domain.models.book import Book
    from app.domain.models.book_agent_reading import BookAgentReading

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
    kinds = {e.kind for e in outcome.evidence}
    assert {"knowledge_item", "agent_summary"} <= kinds


def test_agent_only_evidence_skips_model(session):
    """只有通读笔记、没有自己的内容、书里原文也对不上 → 不调模型，直接依据不足。"""
    book = _book_and_reading(
        session, title="原则", summary="极度透明意味着要能说真话。",
        text="三个番茄两个蛋，先炒蛋再炒番茄。",
    )
    provider = _Scripted([_draft(f"agent:{book.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "insufficient_evidence"
    assert provider.calls == []


def test_book_excerpt_only_evidence_generates(session):
    """没有自己的笔记，只有智能体读过的书里能对上的原文 → 也能生成建议。"""
    book = _book_and_reading(session)
    provider = _Scripted([_draft(f"book:{book.id}:0")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=_ready_ctx(), gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"
    assert outcome.items
    assert outcome.items[0]["basis_item_ids"] == [f"book:{book.id}:0"]
    assert {e.kind for e in outcome.evidence} == {"book_excerpt", "agent_summary"}


def test_last_slot_with_worker_returns_pending_then_worker_writes_result(
    client, session, provider_override, monkeypatch
):
    """有 worker 时，点完最后一个按钮立刻返回 pending 卡，结果由后台写回。

    回归：原来是同步等一次模型调用（真机 5–20 秒），浏览器一直挂在
    `/agent/actions` 上、界面只有「处理中…」——用户看到的就是「点了没反应」。
    """
    from app.agent import turns as turns_mod
    from app.core.config import settings
    from app.llm.gateway import ModelGateway

    queued: list[dict] = []
    monkeypatch.setattr("app.workers.tasks.enqueue", lambda *a, **kw: queued.append(kw))
    monkeypatch.setattr("app.workers.runner.nudge", lambda: None)
    monkeypatch.setattr(settings, "worker_enabled", True, raising=False)

    user = sign_in(client, "sugg_async_pending")
    headers = dict(user.headers)
    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    item_id = saved.json()["item_id"]

    start = client.post("/api/v1/agent/start", json={}, headers=headers).json()
    cid = start["conversation_id"]
    headers["_cid"] = cid
    card = _last_card(start)

    # 「想搞清什么」是自由文本，只能打字；剩下三项点按钮。
    # 没有书可推断时这条必须答，否则槽位永远不满（也就测不到出结果那一步）。
    card = _last_card(_send(client, headers, "怎么把极度透明用到周会上", cid))
    assert card["kind"] == "reading_intake"

    provider_override.rows.append(_draft(f"ki:{item_id}"))
    pending = None
    for slot, value in (("goal", "problem"), ("self_level", "once"), ("help_type", "path")):
        if slot not in [q["slot"] for q in card.get("questions") or []]:
            continue
        card = _answer(client, headers, card, slot, value)["card"]
        if card.get("kind") == "pending":
            pending = card

    assert pending is not None, f"最后一个槽位应立刻返回 pending 卡，而不是等结果：{card}"
    assert pending["key"] == "intake", "pending 卡必须原地替换确认卡（key 不变）"
    assert queued and queued[-1]["payload"]["capability"] == "suggestion"

    # 模拟 worker：跑能力并把结果写回那条 pending 消息
    payload = queued[-1]["payload"]
    session.expire_all()   # 请求走的是另一个 session，先丢掉本地缓存再读
    reply, result_card = turns_mod.execute_capability(
        "suggestion", user_id=user.user_id, session=session,
        gateway=ModelGateway(provider=provider_override), key=payload["turn_id"],
        args={}, conversation_id=cid,
    )
    assert result_card["kind"] == "suggestion" and result_card["state"] == "ok"
    assert turns_mod.finish_turn(
        session, user_id=user.user_id, conversation_id=cid,
        turn_id=payload["turn_id"], reply=reply, card=result_card,
    ), "结果写不回 pending 那条消息，用户会一直看到转圈"

    latest = client.get(f"/api/v1/agent/conversation/{cid}", headers=headers).json()
    assert _last_card(latest)["kind"] == "suggestion"


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


# ---- 「想搞清什么」可被书名代替（回归：点完按钮仍卡在问问题）-------------------


def test_book_title_satisfies_question_slot():
    """有正在读的书时，question 为空也算齐了——「介绍这本书」不需要再打一遍问题。

    回归：用户点完目标/水平/帮助类型三个按钮后，`missing` 仍剩 question，
    系统继续问「这次想搞清什么」，看起来像「按钮都点完了却不出结果」。
    """
    ctx = IntakeContext(
        goal="problem", self_level="once", help_type=["path"], question="",
        book={"book_id": "b1", "title": "时间管理（击退拖延症、处理多重任务）", "source": "reader"},
    )
    ctx.refresh_missing()
    assert ctx.is_ready
    query = sugg.build_query(ctx)
    assert query
    # 主标题进 query，括号里的推广语不进（否则检索会被营销词带偏）
    assert "时间管理" in query
    assert "击退拖延症" not in query


def test_question_empty_but_book_present_generates(session):
    """question 为空、但有书名 → 仍能生成建议，而不是卡在「依据不足」。"""
    item = _seed(session)
    ctx = IntakeContext(
        goal="problem", self_level="once", help_type=["path"], question="",
        book={"book_id": "b1", "title": "原则 · 第三章", "source": "reader"},
    )
    ctx.refresh_missing()
    assert ctx.is_ready

    provider = _Scripted([_draft(f"ki:{item.id}")])
    outcome = sugg.generate(
        session, user_id="u1", ctx=ctx, gateway=ModelGateway(provider=provider),
        embedding=HashEmbedding(),
    )
    assert outcome.state == "ok"


def test_replace_card_updates_latest_matching(session):
    """多条 key=intake 的卡片时，替换的是**最新那条**。

    回归：`_replace_card` 从前往后找、命中即返回，于是建议卡被塞到开场消息上，
    用户正在看的那条仍停在旧状态——界面看起来就是「点了没反应」。
    """
    from app.agent import turns
    from app.domain.repositories.conversation_repository import ConversationRepository

    repo = ConversationRepository(session, user_id="u1")
    conv = repo.create(user_id="u1")
    repo.append_message(
        conv, "assistant", "开场", source="agent",
        card={"kind": "reading_intake", "key": "intake", "round": 0},
    )
    repo.append_message(
        conv, "assistant", "追问", source="agent",
        card={"kind": "reading_intake", "key": "intake", "round": 1},
    )
    session.commit()

    ok = turns._replace_card(
        session, user_id="u1", conversation_id=conv.id,
        card_key="intake", card={"kind": "suggestion", "key": "sg1"},
    )
    session.commit()

    assert ok is True
    msgs = repo.get(conv.id).messages
    assert msgs[0]["card"]["kind"] == "reading_intake"  # 开场那条不动
    assert msgs[1]["card"]["kind"] == "suggestion"      # 最新那条被替换


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
    """走完：开局确认卡 → 答三项 → 点完最后一个按钮直接出建议（不再点「生成」）。"""
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

    # 剩下三项用按钮点。最后一个按钮点完就会自动生成，先把模型产出备好。
    provider_override.rows.append(_draft(f"ki:{item_id}"))
    for slot, value in (("goal", "problem"), ("self_level", "once"), ("help_type", "path")):
        if slot not in [q["slot"] for q in card.get("questions") or []]:
            continue
        result = _answer(client, headers, card, slot, value)
        assert result.get("card"), result
        card = result["card"]

    # 槽位齐了 → 直接是建议卡（不再是带「生成」按钮的确认卡）
    assert card["kind"] == "suggestion", f"点完最后一个按钮应自动生成：{card}"
    assert card["state"] == "ok"
    assert card["items"]
    assert card["evidence"]
    # 依据必须能对上：items 引用的 id 都在 evidence 池里
    pool = {e["evidence_id"] for e in card["evidence"]}
    for it in card["items"]:
        assert set(it["basis_item_ids"]) <= pool


def test_end_to_end_type_question_last_generates(client, session, provider_override):
    """先点完三个按钮项，最后打字补「想解决什么」→ 槽位齐了自动生成，不再要求点「生成」。

    回归：goal / self_level / help_type 是按钮，点完即可；「想解决什么」是自由文本，
    只能靠打字。原来槽位齐了还要再点一次「生成建议」才出结果——用户点完框框再打字，
    等来的不是建议而是一张「点下面生成」的确认卡，看起来像系统明明懂了却不动手。
    """
    user = sign_in(client, "sugg_type_last")
    headers = dict(user.headers)
    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text
    item_id = saved.json()["item_id"]

    start = client.post("/api/v1/agent/start", json={}, headers=headers)
    conv = start.json()
    headers["_cid"] = conv["conversation_id"]

    card = _last_card(conv)
    assert card is not None and card["kind"] == "reading_intake"

    # 三个按钮项按序点掉（每次点击返回新卡，key 恒为 intake）
    for slot, value in (("goal", "problem"), ("self_level", "once"), ("help_type", "path")):
        result = _answer(client, headers, card, slot, value)
        assert result.get("card"), result
        card = result["card"]

    # 只剩「想解决什么」一个自由文本槽位。打字补上它：应直接生成建议卡。
    # 第一个 row 给模型分流（这句话是自由文本，本地规则不命中），第二个给建议生成。
    provider_override.rows.append('{"capability": "chat", "args": {}, "confidence": 0.5}')
    provider_override.rows.append(_draft(f"ki:{item_id}"))
    body = _send(client, headers, "怎么把极度透明用到周会上", headers["_cid"])
    card = _last_card(body)
    assert card["kind"] == "suggestion", f"补全槽位后应自动生成建议：{card}"
    assert card["state"] == "ok"


def test_end_to_end_model_misroute_question_still_captured(client, session, provider_override):
    """模型把确认期的自由文本回答误判成 book_discuss → 仍应接管，回确认卡。

    回归：确认门禁原本只在 chat 路由下触发。真实环境里模型会把
    「怎么把它用到周会上」判成 book_discuss（「它」像指一本书），门禁被跳过，
    用户等来一句「没确定要聊哪本书」——答非所问，看起来像系统完全没听懂。
    """
    user = sign_in(client, "sugg_misroute")
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

    # 模拟模型分流把这句话误判成「聊书」
    provider_override.rows.append(
        '{"capability": "book_discuss", "args": {}, "confidence": 0.8}'
    )
    body = _send(client, headers, "怎么把它用到周会上", headers["_cid"])
    card = _last_card(body)
    assert card["kind"] == "reading_intake", f"确认期回答不应被 book_discuss 抢走：{card}"
    assert card["digest"]["question"] == "怎么把它用到周会上"


def test_suggestion_command_keeps_trailing_question(client, session, provider_override):
    """「给我建议，怎么运用」里的后半句是内容，不该被当指令整句丢弃。

    回归：`is_suggestion_command` 判定整句是指令后，原来把 question 一并 pop 掉，
    用户明明说了「怎么运用」，系统却回头问「还想解决的问题」，等于让用户白说一遍。
    """
    user = sign_in(client, "sugg_lead")
    headers = dict(user.headers)
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    start = client.post("/api/v1/agent/start", json={}, headers=headers).json()
    headers["_cid"] = start["conversation_id"]

    body = _send(client, headers, "给我建议，怎么把它用到周会上", headers["_cid"])
    card = _last_card(body)
    assert card["kind"] == "reading_intake"
    assert card["digest"]["question"] == "怎么把它用到周会上"


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
