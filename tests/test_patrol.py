"""自主巡检测试（第二阶段 C2）：L4 偏离日巡检 + L2 弱真值周体检。

重点：
1. 幂等键按日/周分桶——同日/同周 ensure 只排队一次；
2. 无计划/无偏离是零 LLM 快路径（本地信号排查不烧钱）；
3. 偏离时恰好 1 次归因 + 1 条干预推送 + 1 条事件；
4. 重复护栏：近 N 天已归因过就跳过（防 LLM 成本与干预刷屏）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.domain.models.learning_event import LearningEvent
from app.domain.models.push_job import PushJob
from app.domain.models.user import User
from app.feedback import events
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.workers import handlers as _handlers  # noqa: F401  导入即注册
from app.workers.tasks import run_pending
from app.workers.triggers import (
    TASK_L2_EVAL_WEEKLY,
    TASK_L4_DEVIATION_CHECK,
    ensure_patrols,
    enqueue_patrol,
    l2_eval_weekly_key,
    l4_deviation_key,
)


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


PLAN_ROWS = {
    "plan_generation": [{
        "tasks": [{"week_index": 1, "subject": "读完《数据库索引》并写出三条判断标准", "focus": "补原理"}],
        "rationale": "先补原理",
    }]
}

DEVIATE_ROWS = {
    "deep_reasoning": [{
        "root_cause": "连续多天没有学习行为，计划停住了",
        "adjustment": "把任务拆成每次 20 分钟的最小单元",
        "expected_gain": "单周完成率回升",
        "confidence": 0.8,
    }],
}


def _patch_gateway(monkeypatch, provider: FakeProvider) -> FakeProvider:
    """让 handler 函数内 `from app.llm.gateway import ModelGateway` 拿到带 FakeProvider 的网关。"""
    real = ModelGateway

    def _factory(*args, **kwargs):
        return real(provider=provider)

    monkeypatch.setattr("app.llm.gateway.ModelGateway", _factory)
    return provider


def _emit(session, user_id: str, event_type: str, days_ago: float = 0.0, payload: dict | None = None) -> None:
    from app.domain.repositories.learning_event_repository import LearningEventRepository

    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    LearningEventRepository(session).append(
        user_id=user_id, event_type=event_type, payload=payload or {}, occurred_at=when
    )
    session.commit()


def _seed_plan(session) -> None:
    from app.agent.l4_orchestrator import L4Orchestrator

    orch = L4Orchestrator(ModelGateway(provider=FakeProvider(PLAN_ROWS)), session)
    goal_id = orch.create_goal(user_id="u1", description="目标")
    orch.generate_plan(user_id="u1", goal_id=goal_id)


def _queue_today_patrol(session) -> None:
    enqueue_patrol(session, user_id="u1", task_name=TASK_L4_DEVIATION_CHECK, key=l4_deviation_key("u1"))


# ---------- 幂等键分桶 ----------


def test_deviation_keys_are_day_bucketed():
    d1 = datetime(2026, 9, 29, 1, 0)
    d2 = datetime(2026, 9, 29, 23, 0)
    d3 = datetime(2026, 9, 30, 0, 0)
    assert l4_deviation_key("u1", d1) == l4_deviation_key("u1", d2)  # 同日同键
    assert l4_deviation_key("u1", d1) != l4_deviation_key("u1", d3)  # 换日换键
    assert l4_deviation_key("u1", d1) == "l4:deviation:u1:2026-09-29"


def test_eval_keys_week_bucketed():
    mon = datetime(2026, 9, 28)  # ISO 2026-W40 的周一
    sun = datetime(2026, 10, 4)  # 同周周日
    nxt = datetime(2026, 10, 5)  # 下周一
    assert l2_eval_weekly_key("u1", mon) == l2_eval_weekly_key("u1", sun)
    assert l2_eval_weekly_key("u1", mon) != l2_eval_weekly_key("u1", nxt)


def test_ensure_patrols_queues_once_per_day(session):
    first = ensure_patrols(session, ["u1"])
    second = ensure_patrols(session, ["u1"])

    assert first == 2  # 日巡检 + 周体检各一
    assert second == 0  # 同日/同周 ensure 不再新建


# ---------- 零 LLM 快路径 ----------


def test_patrol_no_plan_costs_zero_llm(session, monkeypatch):
    """无计划：check_deviation 直接 no_plan，deep_reasoning 零调用、零推送。"""
    provider = _patch_gateway(monkeypatch, FakeProvider({**PLAN_ROWS, **DEVIATE_ROWS}))
    _queue_today_patrol(session)

    summary = run_pending(session)

    assert summary.succeeded == 1
    assert "deep_reasoning" not in provider.calls
    assert list(session.scalars(select(PushJob))) == []


def test_patrol_no_deviation_costs_zero_llm(session, monkeypatch):
    """行为活跃 → 本地判定无偏离：不归因、不推送。"""
    _seed_plan(session)
    for _ in range(3):
        _emit(session, "u1", events.KNOWLEDGE_CREATED)  # 今天 3 条行为 → 无偏离
    provider = _patch_gateway(monkeypatch, FakeProvider({**PLAN_ROWS, **DEVIATE_ROWS}))
    _queue_today_patrol(session)

    run_pending(session)

    assert "deep_reasoning" not in provider.calls
    assert list(session.scalars(select(PushJob))) == []


# ---------- 偏离 → 归因 → 干预推送 ----------


def test_patrol_deviation_pushes_intervention_and_records_event(session, monkeypatch):
    _seed_plan(session)
    _emit(session, "u1", events.KNOWLEDGE_CREATED, days_ago=5)  # 5 天前动过 → 偏离
    provider = _patch_gateway(monkeypatch, FakeProvider({**PLAN_ROWS, **DEVIATE_ROWS}))
    _queue_today_patrol(session)

    run_pending(session)

    assert provider.calls.count("deep_reasoning") == 1  # 恰好 1 次归因
    jobs = list(session.scalars(select(PushJob)))
    assert len(jobs) == 1
    assert jobs[0].push_type == "coach"
    assert "计划偏离提醒" in jobs[0].title
    assert "最小单元" in jobs[0].body  # 干预文案含调整建议
    assert any(e.event_type == events.L4_DEVIATION_CHECKED for e in session.scalars(select(LearningEvent)))


def test_patrol_repeat_guard_skips_reanalysis_within_3_days(session, monkeypatch):
    """护栏期内不再归因：持续偏离时 LLM ≤1 次/3 天，干预也不刷屏。"""
    _seed_plan(session)
    _emit(session, "u1", events.KNOWLEDGE_CREATED, days_ago=5)
    _emit(session, "u1", events.L4_DEVIATION_CHECKED, days_ago=1)  # 昨天刚归因过
    provider = _patch_gateway(monkeypatch, FakeProvider({**PLAN_ROWS, **DEVIATE_ROWS}))
    _queue_today_patrol(session)

    run_pending(session)

    assert "deep_reasoning" not in provider.calls  # 护栏拦下再归因
    assert list(session.scalars(select(PushJob))) == []


def test_patrol_quiet_user_intervention_suppressed(session, monkeypatch):
    """quiet 免打扰：干预推送被闸门拦下（coach 不属于重大诊断例外）。"""
    session.add(User(id="u1", username="u_1", password_hash="x", push_frequency="quiet"))
    session.commit()
    _seed_plan(session)
    _emit(session, "u1", events.KNOWLEDGE_CREATED, days_ago=5)
    _patch_gateway(monkeypatch, FakeProvider({**PLAN_ROWS, **DEVIATE_ROWS}))
    _queue_today_patrol(session)

    run_pending(session)

    assert list(session.scalars(select(PushJob))) == []  # 被 quiet 拦下
    # 归因已发生（护栏事件已记）——成本护栏与推送闸门独立
    assert any(e.event_type == events.L4_DEVIATION_CHECKED for e in session.scalars(select(LearningEvent)))


# ---------- 弱真值周体检 ----------


def test_weekly_eval_writes_metrics_event_once(session):
    from app.domain.repositories.conflict_repository import ConflictRepository

    ConflictRepository(session, user_id="u1").create(
        user_id="u1", item_a_id="a", item_b_id="b", claim_a_id="ca", claim_b_id="cb",
        conflict_type="立场对立", confidence=0.9,
    )
    session.commit()
    enqueue_patrol(session, user_id="u1", task_name=TASK_L2_EVAL_WEEKLY, key=l2_eval_weekly_key("u1"))

    run_pending(session)

    rows = [e for e in session.scalars(select(LearningEvent)) if e.event_type == events.L2_EVAL_WEEKLY]
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["n_accepted"] == 0 and payload["n_ignored"] == 0
    assert "precision" in payload and "calibration_by_bucket" in payload


def test_weekly_eval_task_not_rerun_same_week(session):
    enqueue_patrol(session, user_id="u1", task_name=TASK_L2_EVAL_WEEKLY, key=l2_eval_weekly_key("u1"))
    enqueue_patrol(session, user_id="u1", task_name=TASK_L2_EVAL_WEEKLY, key=l2_eval_weekly_key("u1"))
    run_pending(session)

    rows = [e for e in session.scalars(select(LearningEvent)) if e.event_type == events.L2_EVAL_WEEKLY]
    assert len(rows) == 1  # 同周只跑一次（幂等键挡 + 只有一条任务）