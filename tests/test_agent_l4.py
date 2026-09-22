"""L4 路径修正接进对话的测试（全 Mock，不触网）。

L4 被拆成三个能力（`l4_goal` / `l4_plan` / `l4_deviation`），因为**异步是按能力名判定的**：
定目标只写本地库、拆计划调一次模型、偏离检查在 reasoning=on 上跑——合成一个能力就没法
给它们各自选同步还是异步。

这份测试重点钉三件事：

1. **三段不互相抢**（三者的触发词都绕着「计划/目标」转，规则一糊就串）。
2. **前提不足时说清楚差什么**：没目标就别说「生成失败了」，要说「还没有目标」。
3. **慢操作不阻塞**：按建议重排计划走后台，且失败时能**还原原卡**——
   用户不该因为一次执行失败就把已经拿到的偏离分析丢掉。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.agent import turns
from app.api import deps
from app.core.config import settings
from app.domain.models.conversation import Conversation
from app.domain.models.learning_goal import LearningGoal
from app.domain.models.learning_plan import LearningPlan
from app.domain.models.plan_task import PlanTask
from app.domain.models.task_run import TaskRun
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
    PlanTaskRepository,
)
from app.feedback import events
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.helpers import auth_headers, sign_in

GOAL_TEXT = "帮我定一个学习目标：三个月掌握数据分析"
PLAN_TEXT = "生成周计划"
DEVIATION_TEXT = "检查我有没有偏离计划"


class ScriptedProvider(LLMProvider):
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
    scripted = ScriptedProvider()

    def override_gateway():
        return ModelGateway(provider=scripted)

    app.dependency_overrides[deps.get_gateway] = override_gateway
    yield scripted
    app.dependency_overrides.pop(deps.get_gateway, None)


def _send(client, headers, message, conversation_id=None):
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _read(client, headers, conversation_id):
    resp = client.get(f"/api/v1/agent/conversation/{conversation_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _action(client, headers, payload):
    return client.post("/api/v1/agent/actions", json=payload, headers=headers)


def _plan_payload(subject: str = "读完《数据分析入门》并写出三条判断标准") -> str:
    return json.dumps({
        "tasks": [{"week_index": 1, "subject": subject, "focus": "建立框架", "related_item_ids": []}],
        "rationale": "先打基础再上工具",
    })


def _deviation_payload() -> str:
    return json.dumps({
        "root_cause": "最近 7 天没有学习行为，计划停在第一周",
        "adjustment": "把第一周任务拆成每天 15 分钟能做完的小块",
        "expected_gain": "单周完成率从 0/2 提升到 1/2",
        "confidence": 0.7,
    })


def _seed_goal(session, user_id: str, description: str = "三个月掌握数据分析") -> LearningGoal:
    goal = LearningGoalRepository(session, user_id=user_id).create(
        user_id=user_id, description=description
    )
    session.commit()
    return goal


def _seed_plan(session, user_id: str, *, tasks: list[dict] | None = None, version: int = 1):
    goal = _seed_goal(session, user_id)
    plan = LearningPlanRepository(session, user_id=user_id).create(
        user_id=user_id, goal_id=goal.id, content={"rationale": "测试用"}, version=version
    )
    if tasks:
        PlanTaskRepository(session, user_id=user_id).create_many(
            user_id=user_id, plan_id=plan.id, tasks=tasks
        )
    session.commit()
    return goal, plan


# ---- l4_goal：定目标 / 查看 -------------------------------------------------


def test_l4_goal_creates_goal_and_offers_plan_button(client, session, provider):
    headers = auth_headers(client, "l4g_create")
    body = _send(client, headers, GOAL_TEXT)

    assert body["capability"] == "l4_goal"
    card = body["card"]
    assert card["kind"] == "l4_goal"
    assert card["description"] == "三个月掌握数据分析"
    # 「再做一件事」类按钮用 sends（前端发一句话），不走 actions 端点
    assert card["sends"] == [{"label": "生成周计划", "message": "生成周计划"}]
    assert provider.task_types == []          # 定目标不调模型

    session.flush()
    goals = session.scalars(select(LearningGoal)).all()
    assert [g.description for g in goals] == ["三个月掌握数据分析"]


def test_l4_goal_without_description_asks_back(client, session, provider):
    """只说「帮我定个目标」没说定什么 → 反问一句，绝不建一个空目标。"""
    headers = auth_headers(client, "l4g_blank")
    body = _send(client, headers, "帮我定一个目标")

    assert body["card"]["kind"] == "notice"
    assert "具体一点" in body["reply"]
    session.flush()
    assert session.query(LearningGoal).count() == 0


def test_l4_goal_view_without_plan_points_at_the_next_step(client, session, provider):
    """有目标但还没计划 → 告诉用户差哪一步、并给他一句可以直接点的按钮。"""
    user = sign_in(client, "l4g_view")
    _seed_goal(session, user.user_id, "三个月掌握数据分析")

    body = _send(client, user.headers, "我的计划进展如何")

    assert body["capability"] == "l4_goal"
    assert body["card"]["kind"] == "notice"
    assert "三个月掌握数据分析" in body["reply"]
    assert body["card"]["sends"][0]["message"] == "生成周计划"


def test_l4_goal_view_returns_the_current_plan(client, session, provider):
    user = sign_in(client, "l4g_view2")
    _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])

    body = _send(client, user.headers, "我的计划进展如何")

    assert body["card"]["kind"] == "l4_plan"
    assert body["card"]["tasks"][0]["subject"] == "第一周任务"
    assert "已完成" in body["reply"]


# ---- l4_plan：拆周计划 ------------------------------------------------------


def test_l4_plan_generates_weekly_tasks(client, session, provider):
    user = sign_in(client, "l4p_ok")
    _seed_goal(session, user.user_id)
    provider.rows.append(_plan_payload())

    body = _send(client, user.headers, PLAN_TEXT)

    assert body["capability"] == "l4_plan"
    card = body["card"]
    assert card["kind"] == "l4_plan"
    assert card["state"] == "ok"
    assert card["tasks"][0]["week_index"] == 1
    assert card["progress"] == {"total": 1, "done": 0, "pending": 1}
    assert "周任务" in body["reply"]

    session.flush()
    assert session.query(LearningPlan).count() == 1
    assert session.query(PlanTask).count() == 1


def test_l4_plan_without_goal_says_what_is_missing(client, session, provider):
    """没有目标时不能说「生成失败了」——那是与事实相反的提示，用户会以为模型坏了。"""
    headers = auth_headers(client, "l4p_nogoal")
    body = _send(client, headers, PLAN_TEXT)

    assert body["card"]["kind"] == "notice"
    assert body["card"]["title"] == "还没有学习目标"
    assert "先定一个目标" in body["reply"]
    assert provider.task_types == []          # 前提不足就别浪费一次模型调用


def test_l4_plan_degraded_keeps_user_informed(client, session, provider):
    user = sign_in(client, "l4p_bad")
    _seed_goal(session, user.user_id)
    provider.rows.append("这不是 JSON")

    body = _send(client, user.headers, PLAN_TEXT)

    assert body["card"]["kind"] == "l4_plan"
    assert body["card"]["state"] == "degraded"
    assert "失败" in body["reply"]


# ---- l4_deviation：偏离检查 -------------------------------------------------


def test_l4_deviation_without_plan(client, session, provider):
    headers = auth_headers(client, "l4d_noplan")
    body = _send(client, headers, DEVIATION_TEXT)

    assert body["capability"] == "l4_deviation"
    assert body["card"]["state"] == "no_plan"
    assert provider.task_types == []


def test_l4_deviation_ok_returns_signals_and_actions(client, session, provider):
    """有偏离时才调模型（本地无信号不调模型），并给出可操作的两个按钮。"""
    user = sign_in(client, "l4d_ok")
    _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    provider.rows.append(_deviation_payload())

    body = _send(client, user.headers, DEVIATION_TEXT)

    card = body["card"]
    assert card["kind"] == "l4_deviation"
    assert card["state"] == "ok"
    assert card["signals"]["reasons"], "应该带上本地算出的偏离信号"
    assert card["analysis"]["adjustment"]
    assert card["plan_id"]
    assert provider.task_types == ["deep_reasoning"]
    assert "偏离信号" in body["reply"]


def test_l4_deviation_no_deviation_skips_the_model(client, session, provider):
    """本地统计没发现问题时不调模型——否则每次问一句都是一次白花的调用。"""
    user = sign_in(client, "l4d_clean")
    _seed_plan(session, user.user_id)          # 计划尚无任务，排除「主题脱节」那条信号
    # 造一次「刚刚发生过」的学习行为，把「连续无活动」那条信号也排除
    events.record(session, user_id=user.user_id, event_type=events.KNOWLEDGE_CREATED,
                  payload={"item_id": "x"})

    body = _send(client, user.headers, DEVIATION_TEXT)

    assert body["card"]["state"] == "no_deviation"
    assert provider.task_types == []


# ---- 卡片操作：保持原计划（快） ---------------------------------------------


def _attach_deviation_card(session, user_id: str, plan_id: str) -> tuple[str, str]:
    """构造「会话里已经有一张偏离卡」的状态，返回 (会话 id, 卡片 key)。

    真实路径下这张卡是偏离检查跑出来的；这里直接构造，是为了把「慢操作的后台管道」
    与「偏离检查能不能跑出结果」解耦——后者依赖模型输出，不该影响这条测试。
    """
    from app.agent.cards import l4_deviation_card
    from app.domain.repositories.conversation_repository import ConversationRepository

    repo = ConversationRepository(session, user_id=user_id)
    conversation = repo.create(user_id=user_id)
    key = "l4-dev-key-1"
    card = l4_deviation_card(
        key=key, state="ok", plan_id=plan_id,
        signals={"reasons": ["连续 7 天没有学习行为"], "plan_total": 1, "plan_done": 0},
        analysis={"root_cause": "门槛过高", "adjustment": "拆成每天 15 分钟",
                  "expected_gain": "完成率翻倍", "confidence": 0.7},
    )
    repo.append_message(conversation, "user", DEVIATION_TEXT, source=turns.SOURCE)
    repo.append_message(conversation, "assistant", "检测到偏离", source=turns.SOURCE, card=card)
    session.commit()
    return conversation.id, key


def test_keep_plan_records_the_preference(client, session, provider):
    user = sign_in(client, "l4d_keep")
    _goal, plan = _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    cid, key = _attach_deviation_card(session, user.user_id, plan.id)

    resp = _action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l4.adjustment.keep", "target_id": plan.id, "value": "kept",
    })

    assert resp.status_code == 200, resp.text
    assert "偏好" in resp.json()["reply"]
    # 拒绝也要留痕：L5 会用「用户曾拒绝过什么」避免重复打扰
    session.flush()
    decided = LearningEventRepository(session, user_id=user.user_id).list_recent(
        user.user_id, event_type=events.L4_ADJUSTMENT_DECIDED, limit=10
    )
    assert decided and decided[0].payload["accepted"] is False
    # 快操作不该产生后台任务
    assert session.query(TaskRun).filter(TaskRun.task_name == turns.TASK_NAME).count() == 0


def test_keep_plan_rejects_a_missing_plan(client, session, provider):
    user = sign_in(client, "l4d_keep_bad")
    _goal, plan = _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    cid, key = _attach_deviation_card(session, user.user_id, plan.id)

    resp = _action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l4.adjustment.keep", "target_id": "ghost-plan", "value": "kept",
    })
    assert resp.status_code == 422


# ---- 卡片操作：按建议重排（慢 → 后台） --------------------------------------


def test_replan_is_queued_when_worker_enabled(client, session, provider, monkeypatch):
    """慢操作不能在请求里等：先返回 pending 卡，把原卡存进 restore。"""
    monkeypatch.setattr(settings, "worker_enabled", True)
    user = sign_in(client, "l4d_apply")
    _goal, plan = _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    cid, key = _attach_deviation_card(session, user.user_id, plan.id)

    resp = _action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l4.adjustment.apply", "target_id": plan.id, "value": "accepted",
    })
    assert resp.status_code == 200, resp.text
    card = resp.json()["card"]

    assert card["kind"] == "pending"
    assert card["key"] == key, "key 不变，前端才能原地替换"
    assert card["restore"]["kind"] == "l4_deviation", "原卡要留着，失败时还原"
    assert card["action"]["name"] == "l4.adjustment.apply"

    session.flush()
    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == turns.task_key(card["turn_id"]))
    ).first()
    assert row is not None and row.payload["kind"] == "action"


def test_replan_without_worker_runs_inline(client, session, provider):
    """没有 worker 就同步跑：慢，但一定出结果，不会留一张永远转圈的卡。"""
    user = sign_in(client, "l4d_apply_sync")
    _goal, plan = _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    cid, key = _attach_deviation_card(session, user.user_id, plan.id)
    provider.rows.append(_plan_payload("重排后的第一周任务"))

    resp = _action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l4.adjustment.apply", "target_id": plan.id, "value": "accepted",
    })
    assert resp.status_code == 200, resp.text
    card = resp.json()["card"]

    assert card["kind"] == "l4_plan"
    assert card["key"] == key
    assert card["version"] == 2                 # 重排 = 新版本，保留历史版本
    assert card["tasks"][0]["subject"] == "重排后的第一周任务"
    session.flush()
    assert session.query(TaskRun).filter(TaskRun.task_name == turns.TASK_NAME).count() == 0


def test_dead_replan_restores_the_original_card(client, session, provider, monkeypatch):
    """后台失败时把原卡还原回来并说明原因。

    否则用户会因为一次执行失败，把已经拿到的偏离分析也一起丢掉——
    那是比「失败」更糟的体验：他连自己刚才看到了什么都不知道了。
    """
    monkeypatch.setattr(settings, "worker_enabled", True)
    user = sign_in(client, "l4d_apply_dead")
    _goal, plan = _seed_plan(session, user.user_id, tasks=[{"week_index": 1, "subject": "第一周任务"}])
    cid, key = _attach_deviation_card(session, user.user_id, plan.id)

    resp = _action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l4.adjustment.apply", "target_id": plan.id, "value": "accepted",
    })
    turn_id = resp.json()["card"]["turn_id"]

    session.flush()
    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == turns.task_key(turn_id))
    ).first()
    row.status = "dead"
    row.last_error = "boom"
    session.commit()

    card = _read(client, user.headers, cid)["messages"][-1]["card"]
    assert card["kind"] == "l4_deviation", "原卡必须回来"
    assert card["key"] == key
    assert "可以再试一次" in card["note"]


def test_replan_on_the_wrong_card_is_rejected(client, session, provider):
    """动作只能在它自己那张卡上用：拿计划卡去发重排动作必须被拒。"""
    user = sign_in(client, "l4d_wrongcard")
    _seed_goal(session, user.user_id)
    provider.rows.append(_plan_payload())
    body = _send(client, user.headers, PLAN_TEXT)   # 得到一张 l4_plan 卡

    resp = _action(client, user.headers, {
        "conversation_id": body["conversation_id"], "card_key": body["card"]["key"],
        "action": "l4.adjustment.apply", "target_id": body["card"]["plan_id"], "value": "accepted",
    })
    assert resp.status_code == 422
    assert "不支持" in resp.json()["detail"]


def test_conversation_records_the_l4_exchange(client, session, provider):
    user = sign_in(client, "l4_hist")
    _seed_goal(session, user.user_id)
    provider.rows.append(_plan_payload())
    body = _send(client, user.headers, PLAN_TEXT)

    messages = session.get(Conversation, body["conversation_id"]).messages
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[-1]["card"]["kind"] == "l4_plan"
