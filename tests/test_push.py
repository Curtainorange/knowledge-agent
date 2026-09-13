"""推送与抑制服务测试（ADR-14）。

重点验证三道抑制闸门（免打扰 / 去重 / 收敛）都在**本地**可判，不调模型：
- quiet 用户只收「重大诊断」，常规推送全拦
- 同一 content_hash 二次入队被去重挡回
- 同类冲突被忽略 ≥ 阈值后收敛该类推荐
"""
from __future__ import annotations

from sqlalchemy import select

from app.agent.push_service import (
    SUPPRESS_CONVERGED,
    SUPPRESS_DUPLICATE,
    SUPPRESS_QUIET,
    PushService,
    content_hash,
)
from app.core.config import settings
from app.domain.models.conflict import Conflict
from app.domain.models.learning_event import LearningEvent
from app.domain.models.push_job import PushJob
from app.domain.models.push_log import PushLog
from app.domain.models.user import User
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.user_repository import UserRepository
from app.feedback import events


def _service(session):
    return PushService(session)


def _set_frequency(session, user_id: str, frequency: str) -> None:
    user = UserRepository(session).get(user_id)
    if user is None:
        user = User(id=user_id, username=f"u_{user_id}", password_hash="x", push_frequency=frequency)
        session.add(user)
    else:
        user.push_frequency = frequency
    session.commit()


# ---------- 去重指纹 ----------


def test_content_hash_is_deterministic():
    assert content_hash("u1", "conflict", "c1") == content_hash("u1", "conflict", "c1")
    assert content_hash("u1", "conflict", "c1") != content_hash("u1", "conflict", "c2")
    assert content_hash("u1", "conflict", "c1") != content_hash("u2", "conflict", "c1")


# ---------- 抑制决策 ----------


def test_quiet_user_suppresses_regular_push(session):
    _set_frequency(session, "u1", "quiet")
    ok, reason = _service(session).suppress(
        user_id="u1", push_type="brief", content_hash="h1"
    )
    assert ok is True and reason == SUPPRESS_QUIET
    # 重大诊断例外：quiet 用户仍收诊断
    ok2, _ = _service(session).suppress(
        user_id="u1", push_type="diagnosis", content_hash="h2"
    )
    assert ok2 is False


def test_duplicate_content_is_suppressed(session):
    svc = _service(session)
    outcome = svc.enqueue(user_id="u1", push_type="brief", title="t", body="b", subject="week1")
    assert outcome.status == "pending"

    # 同一 subject 再次入队 → 去重抑制
    again = svc.enqueue(user_id="u1", push_type="brief", title="t", body="b", subject="week1")
    assert again.status == "suppressed"
    assert again.reason == SUPPRESS_DUPLICATE


def test_converged_conflict_type_is_suppressed(session):
    # 造一个被忽略 ≥ 阈值次的冲突类型
    repo = ConflictRepository(session, user_id="u1")
    for _ in range(settings.l2_ignore_suppress_threshold):
        c = repo.create(
            user_id="u1", item_a_id="a", item_b_id="b",
            claim_a_id="ca", claim_b_id="cb", conflict_type="立场对立",
        )
        repo.set_state(c, "ignored")
    session.commit()

    ok, reason = _service(session).suppress(
        user_id="u1", push_type="conflict", content_hash="h3", conflict_type="立场对立"
    )
    assert ok is True and reason == SUPPRESS_CONVERGED

    # 其他类型不受影响
    ok2, _ = _service(session).suppress(
        user_id="u1", push_type="conflict", content_hash="h4", conflict_type="结论互斥"
    )
    assert ok2 is False


# ---------- 入队 ----------


def test_enqueue_creates_job_and_log(session):
    outcome = _service(session).enqueue(
        user_id="u1", push_type="brief", title="周简报", body="本周 3 条知识", subject="w1"
    )
    assert outcome.status == "pending" and outcome.job_id

    job = session.scalars(select(PushJob)).first()
    assert job is not None and job.status == "pending"
    assert job.push_type == "brief"

    log = session.scalars(select(PushLog)).first()
    assert log is not None

    # 埋点
    types = {e.event_type for e in session.scalars(select(LearningEvent))}
    assert events.PUSH_SENT in types


def test_mark_delivered(session):
    outcome = _service(session).enqueue(
        user_id="u1", push_type="brief", title="t", body="b", subject="w2"
    )
    _service(session).mark_delivered(user_id="u1", job_id=outcome.job_id)
    job = session.scalars(select(PushJob)).first()
    assert job.status == "delivered"


# ---------- 周简报组装 ----------


def test_build_weekly_digest_counts_locally(session):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 本周两条冲突（一条 unseen）
    repo = ConflictRepository(session, user_id="u1")
    repo.create(user_id="u1", item_a_id="a", item_b_id="b", claim_a_id="ca", claim_b_id="cb",
                conflict_type="立场对立")
    repo.create(user_id="u1", item_a_id="c", item_b_id="d", claim_a_id="cc", claim_b_id="cd",
                conflict_type="结论互斥")
    # 一条知识录入事件
    from app.domain.repositories.learning_event_repository import LearningEventRepository
    LearningEventRepository(session).append(
        user_id="u1", event_type=events.KNOWLEDGE_CREATED, payload={},
        occurred_at=now - timedelta(days=1),
    )
    session.commit()

    digest = _service(session).build_weekly_digest(user_id="u1", now=now)
    assert digest.conflict_total == 2
    assert digest.conflict_unseen == 2
    assert digest.new_items == 1
    assert digest.title  # 周标签标题非空
