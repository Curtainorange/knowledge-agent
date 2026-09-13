"""主动推送调度测试（ADR-14 定时侧）。

周简报 / 月健康报告都靠「幂等键 + worker 轮询」实现，不引入调度器：
- 幂等键按 ISO 周 / 月去重，重复 ensure 不会重复入队
- 处理器只在「有内容」时才真正生成推送（无冲突无新知识 / 无行为数据 → 跳过）
"""
from __future__ import annotations

from sqlalchemy import select

from app.domain.models.push_job import PushJob
from app.domain.models.user import User
from app.feedback import events
from app.workers import handlers  # noqa: F401  导入即注册
from app.workers.tasks import HANDLERS, TaskRun
from app.workers.triggers import (
    ensure_push_schedules,
    enqueue_push,
    push_monthly_key,
    push_weekly_key,
)


def _user(session, user_id="u1", frequency="weekly") -> str:
    u = User(id=user_id, username=f"u_{user_id}", password_hash="x", push_frequency=frequency)
    session.add(u)
    session.commit()
    return user_id


# ---------- 幂等键 ----------


def test_push_keys_are_time_bucketed():
    assert push_weekly_key("u1").startswith("push:weekly:u1:")
    assert push_monthly_key("u1").startswith("push:monthly:u1:")
    assert push_weekly_key("u1") != push_weekly_key("u2")
    assert push_monthly_key("u1") != push_weekly_key("u1")


def test_enqueue_push_is_idempotent(session):
    _user(session, "u1")
    first = enqueue_push("u1", session, monthly=False)
    second = enqueue_push("u1", session, monthly=False)  # 同一 ISO 周 → 不重复入队
    assert first is not None and first.created is True
    assert second is not None and second.created is False


def test_ensure_push_schedules_queues_once_per_user(session):
    _user(session, "u1")
    assert ensure_push_schedules(session, ["u1"]) == 2  # 周 + 月 各一条
    assert ensure_push_schedules(session, ["u1"]) == 0  # 再跑不重复入队


# ---------- 处理器 ----------


def test_weekly_digest_handler_pushes_when_has_content(session):
    _user(session, "u1")
    from app.domain.repositories.learning_event_repository import LearningEventRepository
    LearningEventRepository(session).append(
        user_id="u1", event_type=events.KNOWLEDGE_CREATED, payload={}
    )
    session.commit()

    handlers.push_weekly_digest({"user_id": "u1"}, session)
    jobs = session.scalars(select(PushJob)).all()
    assert len(jobs) == 1
    assert jobs[0].push_type == "brief"


def test_weekly_digest_handler_skips_when_no_content(session):
    _user(session, "u1")  # 无知识、无冲突
    handlers.push_weekly_digest({"user_id": "u1"}, session)
    assert session.scalars(select(PushJob)).first() is None


def test_monthly_health_handler_pushes_on_diagnosis(monkeypatch, session):
    _user(session, "u1")
    from app.agent import l5_orchestrator

    class FakeResult:
        state = "ok"
        diagnosis_id = "d1"
        pattern = "高收藏低完成"
        root_cause = "完成比 12%"
        suggested_action = "每天只读一篇"

    monkeypatch.setattr(
        l5_orchestrator.L5Orchestrator, "diagnose", lambda self, user_id: FakeResult()
    )
    handlers.push_monthly_health({"user_id": "u1"}, session)
    job = session.scalars(select(PushJob)).first()
    assert job is not None and job.push_type == "diagnosis"


def test_monthly_health_handler_skips_when_empty(monkeypatch, session):
    _user(session, "u1")
    from app.agent import l5_orchestrator

    class FakeEmpty:
        state = "empty"

    monkeypatch.setattr(
        l5_orchestrator.L5Orchestrator, "diagnose", lambda self, user_id: FakeEmpty()
    )
    handlers.push_monthly_health({"user_id": "u1"}, session)
    assert session.scalars(select(PushJob)).first() is None


def test_push_handlers_registered():
    assert "push_weekly_digest" in HANDLERS
    assert "push_monthly_health" in HANDLERS
