"""事件驱动反应测试（第二阶段 C3）：learning_events → 任务/推送映射。

重点：
1. 映射表内的事件触发反应（通读完成→扫描、诊断→即时推送）；
2. 同事件幂等（react:{event.id} 账本）——重复扫描窗口零副作用；
3. 映射外事件、窗口外事件零动作；
4. 诊断即时推送与月健康推送同 content_hash 互相去重。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis
from app.domain.models.learning_event import LearningEvent
from app.domain.models.push_job import PushJob
from app.domain.models.task_run import TaskRun
from app.feedback import events
from app.workers import handlers as _handlers  # noqa: F401  导入即注册
from app.workers.reactions import ensure_event_reactions
from app.workers.tasks import run_pending


def _emit(session, user_id: str, event_type: str, payload: dict | None = None, hours_ago: float = 0.0) -> LearningEvent:
    from app.domain.repositories.learning_event_repository import LearningEventRepository

    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    event = LearningEventRepository(session).append(
        user_id=user_id, event_type=event_type, payload=payload or {}, occurred_at=when
    )
    session.commit()
    return event


def _tasks(session, name: str) -> list[TaskRun]:
    return [r for r in session.scalars(select(TaskRun)) if r.task_name == name]


def test_book_digest_done_enqueues_l2_scan(session):
    _emit(session, "u1", events.BOOK_DIGEST_DONE, {"book_id": "b1", "chunks": 3})
    assert ensure_event_reactions(session, ["u1"]) == 1  # 排出 1 条反应任务

    run_pending(session)  # 反应任务执行 → enqueue_l2_scan

    scans = _tasks(session, "l2_scan")
    assert len(scans) == 1  # 小时桶内的实时扫描已排队
    assert scans[0].payload.get("reason") == "realtime"


def test_same_hour_digest_merges_into_one_scan(session):
    """两本通读同小时完成 → 反应各自跑，但扫描小时桶合并成 1 个任务。"""
    _emit(session, "u1", events.BOOK_DIGEST_DONE, {"book_id": "b1"})
    _emit(session, "u1", events.BOOK_DIGEST_DONE, {"book_id": "b2"})
    ensure_event_reactions(session, ["u1"])
    run_pending(session)

    assert len(_tasks(session, "event_reaction")) == 2  # 两个事件各一条反应
    assert len(_tasks(session, "l2_scan")) == 1  # 但扫描合并


def test_diagnosis_created_pushes_immediately(session):
    diag = CognitiveDiagnosis(
        user_id="u1", pattern="高收藏低完成", root_cause="收藏即满足",
        confidence=0.7, suggested_action="每收藏 3 条先读完 1 条",
        reasoning_chain="r", status="pending",
    )
    session.add(diag)
    session.commit()
    _emit(session, "u1", events.L5_DIAGNOSIS_CREATED, {"diagnosis_id": diag.id, "pattern": diag.pattern})
    ensure_event_reactions(session, ["u1"])
    run_pending(session)

    jobs = list(session.scalars(select(PushJob)))
    assert len(jobs) == 1
    assert jobs[0].push_type == "diagnosis"
    assert "收藏即满足" in jobs[0].body  # 正文从诊断行回查（事件只存元数据）


def test_diagnosis_push_dedups_against_monthly(session):
    """同 diagnosis_id 的二次反应被 content_hash 拦下（与月推送互斥）。"""
    diag = CognitiveDiagnosis(
        user_id="u1", pattern="p", root_cause="r", confidence=0.6,
        suggested_action="a", reasoning_chain="r", status="pending",
    )
    session.add(diag)
    session.commit()
    e1 = _emit(session, "u1", events.L5_DIAGNOSIS_CREATED, {"diagnosis_id": diag.id})
    e2 = _emit(session, "u1", events.L5_DIAGNOSIS_CREATED, {"diagnosis_id": diag.id})
    assert e1.id != e2.id  # 两条不同事件（例如月推送与反应各记了一次）
    ensure_event_reactions(session, ["u1"])
    run_pending(session)

    assert len(_tasks(session, "event_reaction")) == 2  # 各自都跑了反应……
    assert len(list(session.scalars(select(PushJob)))) == 1  # 但推送被去重成 1 条


def test_reaction_idempotent_per_event_id(session):
    event = _emit(session, "u1", events.BOOK_DIGEST_DONE, {"book_id": "b1"})
    assert ensure_event_reactions(session, ["u1"]) == 1
    assert ensure_event_reactions(session, ["u1"]) == 0  # 同事件不重复排反应

    rows = _tasks(session, "event_reaction")
    assert len(rows) == 1
    assert rows[0].idempotency_key == f"react:{event.id}:{events.BOOK_DIGEST_DONE}"


def test_unmapped_event_types_are_ignored(session):
    _emit(session, "u1", events.KNOWLEDGE_CREATED)
    _emit(session, "u1", events.L2_JUDGMENT_REVIEWED)  # 刻意不映射（走状态表）
    _emit(session, "u1", events.L2_CONFLICT_FEEDBACK)

    assert ensure_event_reactions(session, ["u1"]) == 0
    assert _tasks(session, "event_reaction") == []


def test_old_events_outside_window_not_scanned(session):
    _emit(session, "u1", events.BOOK_DIGEST_DONE, {"book_id": "b1"}, hours_ago=72)  # 超 48h 窗口
    assert ensure_event_reactions(session, ["u1"]) == 0