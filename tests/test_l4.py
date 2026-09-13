"""L4 路径修正测试（UC-L4-01 计划生成 / UC-L4-02 偏离监测与干预）。

重点：
1. **偏离信号必须来自本地统计**，模型只做归因（所以信号断言不依赖 FakeProvider 的措辞）；
2. 无偏离时**不调模型**（否则每次轮询都是白花的调用）；
3. 用户拒绝也必须留痕（偏好记录是 L5 诊断的输入），且不产生新计划版本。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agent.l4_orchestrator import L4Orchestrator
from app.domain.models.learning_event import LearningEvent
from app.domain.models.learning_plan import LearningPlan
from app.domain.models.plan_task import PlanTask
from app.feedback import events
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from tests.helpers import auth_headers


class FakeProvider(LLMProvider):
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
            text = json.dumps({"tasks": [], "rationale": ""})
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning)


def _orch(session, rows=None, provider=None) -> L4Orchestrator:
    return L4Orchestrator(ModelGateway(provider=provider or FakeProvider(rows or {})), session)


PLAN_ROWS = {
    "plan_generation": [{
        "tasks": [
            {"week_index": 1, "subject": "读完《数据库索引》并写出三条判断标准", "focus": "补原理"},
            {"week_index": 2, "subject": "用真实查询验证索引选择性", "focus": "落地"},
        ],
        "rationale": "先补原理再实战",
    }]
}


def _set_plan_tasks_done(session, plan_id: str, status: str) -> None:
    for task in session.scalars(select(PlanTask).where(PlanTask.plan_id == plan_id)):
        task.status = status
    session.commit()


def _emit(session, user_id: str, event_type: str, days_ago: float = 0.0) -> None:
    """直接造历史事件（occurred_at 可控），用于构造各种行为分布。"""
    from app.domain.repositories.learning_event_repository import LearningEventRepository

    repo = LearningEventRepository(session)
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    repo.append(user_id=user_id, event_type=event_type, payload={}, occurred_at=when)
    session.commit()


# ---------- UC-L4-01 目标与计划 ----------


def test_create_goal_and_generate_plan(session):
    orch = _orch(session, PLAN_ROWS)
    goal_id = orch.create_goal(user_id="u1", description="三周掌握数据库索引", priority="high")

    result = orch.generate_plan(user_id="u1", goal_id=goal_id)
    assert result is not None and result.state == "ok"
    view = result.view
    assert view.version == 1
    assert [t["week_index"] for t in view.tasks] == [1, 2]
    assert view.progress == {"total": 2, "done": 0, "pending": 2}
    assert view.rationale == "先补原理再实战"

    # 事件留痕
    types = {e.event_type for e in session.scalars(select(LearningEvent))}
    assert events.L4_GOAL_CREATED in types
    assert events.L4_PLAN_GENERATED in types


def test_generate_plan_for_unknown_goal(session):
    assert _orch(session, PLAN_ROWS).generate_plan(user_id="u1", goal_id="不存在") is None


def test_plan_generation_failure_is_reported(session):
    orch = _orch(session, {"plan_generation": ["坏 JSON {"]})
    goal_id = orch.create_goal(user_id="u1", description="目标")
    result = orch.generate_plan(user_id="u1", goal_id=goal_id)
    assert result is not None and result.state == "degraded"
    assert "计划生成失败" in result.note
    assert result.view is None  # 此前从未生成过计划


def test_regenerate_bumps_version(session):
    orch = _orch(session, PLAN_ROWS)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    first = orch.generate_plan(user_id="u1", goal_id=goal_id)
    second = orch.generate_plan(user_id="u1", goal_id=goal_id)
    assert (first.view.version, second.view.version) == (1, 2)
    # 历史版本保留（便于回溯被推翻的路径）
    versions = [p.version for p in session.scalars(select(LearningPlan).order_by(LearningPlan.version))]
    assert versions == [1, 2]


def test_plan_tasks_capped(session):
    from app.core.config import settings

    many = {"tasks": [{"week_index": i, "subject": f"任务 {i}"} for i in range(1, 30)], "rationale": ""}
    orch = _orch(session, {"plan_generation": [many]})
    goal_id = orch.create_goal(user_id="u1", description="大目标")
    view = orch.generate_plan(user_id="u1", goal_id=goal_id).view
    assert len(view.tasks) == settings.l4_max_tasks


# ---------- UC-L4-02 偏离监测 ----------


def test_check_without_plan(session):
    report = _orch(session).check_deviation(user_id="u1")
    assert report.state == "no_plan"


def test_no_deviation_does_not_call_model(session):
    """计划刚建、行为活跃 → 本地判定无偏离，不该产生任何模型调用。"""
    provider = FakeProvider(PLAN_ROWS)
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)
    provider.calls.clear()

    for _ in range(3):
        _emit(session, "u1", events.KNOWLEDGE_CREATED)
    report = orch.check_deviation(user_id="u1")

    assert report.state == "no_deviation"
    assert report.signals.idle_days == 0
    assert provider.calls == []  # 未触发归因调用


def test_idle_days_triggers_deviation_and_analysis(session):
    provider = FakeProvider({
        **PLAN_ROWS,
        "deep_reasoning": [{
            "root_cause": "最近 7 天只有 0 次学习行为，计划第一周就停住了",
            "adjustment": "把第一周任务拆成每次 20 分钟的最小单元",
            "expected_gain": "单周完成率从 0/2 提升到 1/2",
            "confidence": 0.82,
        }],
    })
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)
    _emit(session, "u1", events.KNOWLEDGE_CREATED, days_ago=5)  # 5 天前动过

    report = orch.check_deviation(user_id="u1")
    assert report.state == "ok"
    assert report.signals.idle_days == 5
    assert any("没有学习行为" in r for r in report.signals.reasons)
    assert report.analysis is not None
    assert report.analysis.confidence == 0.82
    assert "deep_reasoning" in provider.calls


def test_no_activity_in_window_is_a_reason(session):
    provider = FakeProvider({
        **PLAN_ROWS,
        "deep_reasoning": [{"root_cause": "r", "adjustment": "a", "expected_gain": "g", "confidence": 0.6}],
    })
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)

    report = orch.check_deviation(user_id="u1")
    assert report.state == "ok"
    assert report.signals.idle_days is None
    assert any("连续" in r for r in report.signals.reasons)


def test_activity_drop_is_detected(session):
    provider = FakeProvider({
        **PLAN_ROWS,
        "deep_reasoning": [{"root_cause": "r", "adjustment": "a", "expected_gain": "g", "confidence": 0.6}],
    })
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)

    # 上一窗口 6 次，本窗口 1 次
    for _ in range(6):
        _emit(session, "u1", events.L1_MINE, days_ago=9)
    _emit(session, "u1", events.L1_MINE, days_ago=1)

    report = orch.check_deviation(user_id="u1")
    assert any("下降" in r for r in report.signals.reasons)


def test_analysis_failure_degrades_but_keeps_signals(session):
    provider = FakeProvider({**PLAN_ROWS, "deep_reasoning": ["坏 JSON {"]})
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)

    report = orch.check_deviation(user_id="u1")
    assert report.state == "degraded"
    assert report.signals is not None and report.signals.reasons
    assert "归因分析失败" in report.note


# ---------- 用户决定 ----------


def test_accepting_adjustment_replans_with_new_version(session):
    provider = FakeProvider({
        **PLAN_ROWS,
        "plan_generation": [
            PLAN_ROWS["plan_generation"][0],
            {"tasks": [{"week_index": 1, "subject": "每次 20 分钟读完两节"}], "rationale": "降门槛"},
        ],
    })
    orch = _orch(session, provider=provider)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    view = orch.generate_plan(user_id="u1", goal_id=goal_id).view

    ok, message = orch.decide_adjustment(user_id="u1", plan_id=view.plan_id, accepted=True)
    assert ok and "重排" in message

    latest = orch.latest_plan_view(user_id="u1")
    assert latest.version == 2
    assert latest.tasks[0]["subject"] == "每次 20 分钟读完两节"
    assert latest.rationale == "降门槛"


def test_rejecting_adjustment_keeps_plan_and_records_preference(session):
    orch = _orch(session, PLAN_ROWS)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    view = orch.generate_plan(user_id="u1", goal_id=goal_id).view

    ok, message = orch.decide_adjustment(user_id="u1", plan_id=view.plan_id, accepted=False)
    assert ok and "保持原计划" in message

    latest = orch.latest_plan_view(user_id="u1")
    assert latest.version == 1  # 未产生新版本
    versions = list(session.scalars(select(LearningPlan.version)))
    assert versions == [1]

    decided = [
        e for e in session.scalars(select(LearningEvent))
        if e.event_type == events.L4_ADJUSTMENT_DECIDED
    ]
    assert decided and decided[-1].payload["accepted"] is False


def test_decide_for_unknown_plan(session):
    ok, message = _orch(session).decide_adjustment(user_id="u1", plan_id="不存在", accepted=True)
    assert ok is False
    assert "不存在" in message


# ---------- API ----------


def test_api_goal_plan_and_deviation_flow(client):
    from app.api import deps
    from app.main import app

    fake = FakeProvider({
        **PLAN_ROWS,
        "deep_reasoning": [{
            "root_cause": "最近 7 天没有学习行为", "adjustment": "降低单次门槛",
            "expected_gain": "完成率提升", "confidence": 0.7,
        }],
    })
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=fake)
    try:
        headers = auth_headers(client, "l4_api_user")

        created = client.post(
            "/api/v1/l4/goals",
            json={"description": "三周掌握数据库索引", "priority": "high"},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        goal_id = created.json()["goal_id"]

        assert client.get("/api/v1/l4/goals", headers=headers).json()["total"] == 1

        plan = client.post(f"/api/v1/l4/goals/{goal_id}/plan", headers=headers)
        assert plan.status_code == 200, plan.text
        body = plan.json()
        assert body["version"] == 1 and len(body["tasks"]) == 2

        latest = client.get("/api/v1/l4/plan", headers=headers).json()
        assert latest["plan_id"] == body["plan_id"]

        # 无学习行为 → 触发偏离与归因
        deviation = client.post("/api/v1/l4/deviation/check", headers=headers)
        assert deviation.status_code == 200, deviation.text
        dev_body = deviation.json()
        assert dev_body["state"] == "ok"
        assert dev_body["root_cause"] and dev_body["adjustment"]
        assert dev_body["signals"]["reasons"]

        # 拒绝 → 保持原计划
        rejected = client.post(
            f"/api/v1/l4/deviation/{body['plan_id']}/decision",
            json={"accepted": False}, headers=headers,
        )
        assert rejected.status_code == 200
        assert "保持原计划" in rejected.json()["message"]
        assert rejected.json()["plan"]["version"] == 1

        # 404：不存在的目标
        assert client.post("/api/v1/l4/goals/nope/plan", headers=headers).status_code == 404

        # 越权：B 用户看不到 A 的目标，也不能给 A 的计划做决定
        headers_b = auth_headers(client, "l4_api_other")
        assert client.get("/api/v1/l4/goals", headers=headers_b).json()["total"] == 0
        assert client.post(
            f"/api/v1/l4/deviation/{body['plan_id']}/decision",
            json={"accepted": True}, headers=headers_b,
        ).status_code == 403
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)


def test_api_l4_requires_auth(client):
    assert client.get("/api/v1/l4/plan").status_code == 401
    assert client.post("/api/v1/l4/goals", json={"description": "x"}).status_code == 401