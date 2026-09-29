"""异步任务框架测试：幂等入队 / 领取执行 / 重试 / 死信 / 触发链路键。

全程不触真实模型：`l2_scan` 处理器在无 KEY 时走 MockProvider，
其输出非 JSON → L2 优雅降级（extraction_failures+1），任务本身仍算成功。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.domain.models.task_run import TaskRun
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.user_repository import UserRepository
from app.workers import handlers as _handlers  # noqa: F401  导入即注册 l2_scan 处理器
from app.workers.tasks import HANDLERS, _utcnow, enqueue, run_pending

CALLS: list[dict] = []


def _register_fixtures() -> None:
    """测试用处理器只需注册一次（HANDLERS 是模块级全局）。"""
    if "test_echo" in HANDLERS:
        return
    HANDLERS["test_echo"] = lambda payload, session: CALLS.append(payload)

    def boom(payload, session):
        raise RuntimeError("boom")

    HANDLERS["test_boom"] = boom


_register_fixtures()


def _rows(session) -> list[TaskRun]:
    return list(session.scalars(select(TaskRun).order_by(TaskRun.created_at.asc())))


# ---------- 幂等入队 ----------


def test_enqueue_is_idempotent(session):
    first = enqueue(session, task_name="test_echo", user_id="u1", payload={"a": 1}, idempotency_key="k1")
    second = enqueue(session, task_name="test_echo", user_id="u1", payload={"a": 2}, idempotency_key="k1")

    assert first.created is True
    assert second.created is False
    assert second.task_id == first.task_id
    assert len(_rows(session)) == 1  # 同 key 只允许一行（唯一约束兜底）


def test_enqueue_different_keys_create_separate_rows(session):
    enqueue(session, task_name="test_echo", user_id="u1", idempotency_key="k1")
    enqueue(session, task_name="test_echo", user_id="u1", idempotency_key="k2")
    assert len(_rows(session)) == 2


# ---------- 领取与执行 ----------


def test_run_pending_executes_handler_and_marks_succeeded(session):
    enqueue(session, task_name="test_echo", user_id="u1", payload={"hello": "world"}, idempotency_key="k-run")
    summary = run_pending(session)

    assert summary.claimed == 1
    assert summary.succeeded == 1
    assert CALLS[-1] == {"hello": "world"}

    row = _rows(session)[0]
    assert row.status == "succeeded"
    assert row.attempts == 1
    assert row.finished_at is not None


def test_handler_failure_retries_then_goes_dead(session):
    enqueue(
        session, task_name="test_boom", user_id="u1",
        idempotency_key="k-boom", max_attempts=2,
    )
    first = run_pending(session)
    assert first.retried == 1
    assert first.dead == 0
    row = _rows(session)[0]
    assert row.status == "pending"  # 留在队列，下轮再试
    assert row.attempts == 1
    assert "boom" in row.last_error

    second = run_pending(session)
    assert second.dead == 1
    assert _rows(session)[0].status == "dead"  # 重试耗尽 → 死信，保留现场
    assert _rows(session)[0].attempts == 2


def test_unregistered_task_does_not_crash_worker(session):
    enqueue(session, task_name="no_such_task", user_id="u1", idempotency_key="k-unknown")
    summary = run_pending(session)
    assert summary.claimed == 1
    row = _rows(session)[0]
    assert row.status == "pending"
    assert "未注册" in row.last_error


def test_succeeded_task_is_not_run_twice(session):
    calls: list[int] = []
    # 直接写表避免 register() 的重复注册保护在重复运行时报错
    HANDLERS.setdefault("test_once", lambda payload, session: calls.append(1))

    enqueue(session, task_name="test_once", user_id="u1", idempotency_key="k-once")
    run_pending(session)
    run_pending(session)
    assert len(calls) == 1


# ---------- 触发链路 ----------


def test_realtime_key_buckets_by_hour():
    from app.workers.triggers import realtime_key

    morning = datetime(2026, 9, 13, 10, 5, tzinfo=timezone.utc)
    same_hour = datetime(2026, 9, 13, 10, 59, tzinfo=timezone.utc)
    next_hour = datetime(2026, 9, 13, 11, 1, tzinfo=timezone.utc)
    assert realtime_key("u1", morning) == realtime_key("u1", same_hour)
    assert realtime_key("u1", morning) != realtime_key("u1", next_hour)
    assert realtime_key("u1", morning) != realtime_key("u2", morning)


def test_weekly_key_is_stable_within_iso_week():
    from app.workers.triggers import weekly_key

    monday = datetime(2026, 9, 7, 8, 0, tzinfo=timezone.utc)
    sunday = datetime(2026, 9, 13, 23, 0, tzinfo=timezone.utc)
    next_monday = datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc)
    assert weekly_key("u1", monday) == weekly_key("u1", sunday)
    assert weekly_key("u1", monday) != weekly_key("u1", next_monday)


def test_ingest_enqueues_scan_and_merges_within_hour(session):
    """录入后自动排扫描；同一小时重复录入只排一次（成本保护）。"""
    from app.ingestion.service import IngestionService
    from app.retrieval.embedding import HashEmbedding

    svc = IngestionService(session, HashEmbedding())
    svc.add_knowledge(user_id="u1", title="A", content="内容 A")
    rows = [r for r in _rows(session) if r.task_name == "l2_scan"]
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].idempotency_key.startswith("l2:realtime:u1:")

    svc.add_knowledge(user_id="u1", title="B", content="内容 B")
    assert len([r for r in _rows(session) if r.task_name == "l2_scan"]) == 1  # 合并

    svc.add_knowledge(user_id="u2", title="C", content="内容 C")
    assert len([r for r in _rows(session) if r.task_name == "l2_scan"]) == 2  # 用户间互不影响


def test_run_once_enqueues_weekly_scan_only_once(session, monkeypatch):
    """周扫：同一周内反复调用 ensure 只会排一次任务。"""
    from app.core.config import settings
    from app.workers.runner import run_once

    # 聚焦 L2 周扫幂等，关闭推送调度（推送调度在 test_push_schedule 单独测）
    monkeypatch.setattr(settings, "push_schedule_enabled", False)
    monkeypatch.setattr(settings, "patrol_enabled", False)  # 巡检在 test_patrol 单独测
    monkeypatch.setattr(settings, "coach_enabled", False)  # 教练聚合在 test_coach 单独测

    users = UserRepository(session)
    users.create_user(username="wk_a", password_hash="x")
    users.create_user(username="wk_b", password_hash="x")
    session.commit()

    first = run_once(session)
    weekly = [r for r in _rows(session) if r.task_name == "l2_scan"]
    assert len(weekly) == 2
    assert first.succeeded == 2  # 处理器跑通（Mock 下 L2 优雅降级但任务成功）

    run_once(session)
    assert len([r for r in _rows(session) if r.task_name == "l2_scan"]) == 2  # 不重复入队
    assert all(r.status == "succeeded" for r in _rows(session))


def test_l2_scan_task_end_to_end(session):
    """l2_scan 处理器真实跑通编排层（Mock 模型下不崩、任务判成功）。"""
    KnowledgeRepository(session, user_id="u9").create(
        user_id="u9", title="数据库索引", content="B+树与哈希索引"
    )
    session.commit()

    enqueue(
        session, task_name="l2_scan", user_id="u9",
        payload={"user_id": "u9", "reason": "realtime"},
        idempotency_key="k-l2-e2e",
    )
    summary = run_pending(session)

    assert summary.succeeded == 1
    row = _rows(session)[0]
    assert row.status == "succeeded"
    assert row.last_error == ""


def test_worker_start_is_noop_when_disabled():
    """测试环境关闭 worker：不起后台线程（避免轮询写库干扰断言）。"""
    from app.workers.runner import start_worker

    assert start_worker() is False


# ---------- stale-running 回收（崩溃悬挂的自愈）----------


def _make_stale_running(session, *, key: str, attempts: int = 0, max_attempts: int = 3, task_name: str = "test_echo") -> TaskRun:
    """构造崩溃遗留的 running 行：状态 running、updated_at 老于回收阈值。"""
    enqueue(session, task_name=task_name, user_id="u1", payload={"k": key},
            idempotency_key=key, max_attempts=max_attempts)
    row = session.scalars(select(TaskRun).where(TaskRun.idempotency_key == key)).one()
    row.status = "running"
    row.attempts = attempts
    row.updated_at = _utcnow() - timedelta(seconds=3600)  # 远超 task_stale_seconds=600
    session.commit()
    return row


def test_stale_running_row_is_reclaimed_to_pending(session):
    row = _make_stale_running(session, key="k-stale")
    summary = run_pending(session)

    assert summary.claimed == 1 and summary.succeeded == 1  # 回收后被领走执行
    assert CALLS[-1] == {"k": "k-stale"}
    session.refresh(row)
    assert row.status == "succeeded"


def test_fresh_running_row_is_not_reclaimed(session):
    enqueue(session, task_name="test_echo", user_id="u1", payload={}, idempotency_key="k-fresh")
    row = session.scalars(select(TaskRun).where(TaskRun.idempotency_key == "k-fresh")).one()
    row.status = "running"  # updated_at = 刚刚，未超阈值
    session.commit()

    summary = run_pending(session)

    assert summary.claimed == 0  # 未超时的 running 不动、不被重复领
    session.refresh(row)
    assert row.status == "running"


def test_reclaimed_task_runs_and_can_still_go_dead(session):
    # attempts=2 是崩溃前的进度：回收保留 attempts，领取 +1=3 ≥ max=3 → 失败即死信
    row = _make_stale_running(session, key="k-dead", attempts=2, max_attempts=3, task_name="test_boom")
    summary = run_pending(session)

    assert summary.claimed == 1 and summary.dead == 1
    session.refresh(row)
    assert row.status == "dead"
    assert row.attempts == 3  # 从 2 保留累加，不是归零重来


def test_reclaim_marks_last_error(session):
    from app.workers.tasks import _reclaim_stale

    row = _make_stale_running(session, key="k-mark", attempts=1)

    reclaimed = _reclaim_stale(session)

    assert reclaimed == 1
    session.refresh(row)
    assert row.status == "pending"
    assert row.last_error == "stale running reclaimed"
    assert row.attempts == 1  # attempts 保留