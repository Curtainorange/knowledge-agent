"""L2 主动推送测试：高置信冲突 → 通知（L2「被动」的最后一块）。

与 `test_push.py` 的分工：那边测**推送服务本身**（三道抑制闸门、去重、入队），
这边测**L2 扫描到通知的接线**——推不推、推什么、什么时候不推。

关键边界（每条都有对应用例）：
- 只推**新产生**的冲突：重扫幂等不重复推、用户显式翻案不推
- 门槛是**校准后**的置信度，且与复核上沿（0.75）对齐：自己没把握的不打扰用户
- 三道抑制闸门（免打扰 / 去重 / 同类收敛）由 `PushService` 统一把守，这里只验其中一条
- 推送是**旁路**：入队抛异常不得反噬扫描（ADR-14）
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.agent.l2_orchestrator import L2Orchestrator
from app.core.config import settings
from app.domain.models.conflict import Conflict
from app.domain.models.push_job import PushJob
from app.domain.models.user import User
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider


class FakeProvider(LLMProvider):
    """按 task_type 回放预设响应；队列空时给安全默认值（与 test_l2 同构）。"""

    def __init__(self, rows_by_task: dict[str, list] | None = None) -> None:
        self.rows_by_task = {k: list(v) for k, v in (rows_by_task or {}).items()}
        self.calls: list[str] = []

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        self.calls.append(task_type)
        rows = self.rows_by_task.get(task_type)
        if rows:
            row = rows.pop(0)
            text = row if isinstance(row, str) else json.dumps(row)
        elif task_type == "batch_extraction":
            text = json.dumps({"claims": []})
        else:
            text = json.dumps({"relation": "无关", "confidence": 0.9})
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


CONFLICT_ROW = {
    "relation": "矛盾", "conflict_type": "立场对立",
    "detail": "A 主张坚守三年，B 主张每月掉头", "suggestion": "生成决策哲学对比表",
    "confidence": 0.9,
}


def _orchestrator(session, rows_by_task) -> L2Orchestrator:
    return L2Orchestrator(ModelGateway(provider=FakeProvider(rows_by_task)), session)


def _two_same_topic_items(session) -> dict[str, list]:
    """两条同主题、立场相反的条目 + 对应的主张提取预案。"""
    repo = KnowledgeRepository(session, user_id="u1")
    repo.create(user_id="u1", title="长期主义者的决策框架", content="坚守既定战略。")
    repo.create(user_id="u1", title="敏捷思维", content="根据反馈快速掉头。")
    session.commit()
    return {
        "batch_extraction": [
            {"claims": [{"statement": "应当坚守既定战略至少三年不因短期波动而动摇",
                         "topic": "决策策略", "polarity": 1, "strength": 0.9}]},
            {"claims": [{"statement": "应当根据市场反馈每月调整方向快速试错迭代",
                         "topic": "决策策略", "polarity": -1, "strength": 0.9}]},
        ],
    }


def _push_jobs(session) -> list[PushJob]:
    return list(session.scalars(select(PushJob)).all())


# ---------- 推什么 ----------


def test_high_confidence_conflict_enqueues_notification(session):
    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [dict(CONFLICT_ROW)]
    result = _orchestrator(session, rows).scan(user_id="u1")
    assert result.conflicts_found == 1

    jobs = _push_jobs(session)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.push_type == "conflict"
    assert job.status == "pending"
    # payload 是通知页「去查看」的落点，也是审计时回溯冲突的凭据
    assert job.payload["conflict_id"] == result.conflict_ids[0]
    assert job.payload["conflict_type"] == "立场对立"
    assert job.payload["href"] == "/conflicts.html"
    # 正文明细 + 建议动作都要进通知，否则用户只看到「有冲突」还得自己回去找
    assert "坚守三年" in job.body
    assert "生成决策哲学对比表" in job.body
    # 标题带两侧来源，便于一眼判断值不值得点开
    assert "长期主义者的决策框架" in job.title


# ---------- 什么时候不推 ----------


def test_low_confidence_conflict_stored_but_not_pushed(session, monkeypatch):
    """0.6 校准后约 0.64：够入库（≥0.5），不够打扰用户（<0.75）。"""
    monkeypatch.setattr(settings, "l2_review_enabled", False)  # 隔离复核，只看推送门槛
    rows = _two_same_topic_items(session)
    low = dict(CONFLICT_ROW, confidence=0.6)
    rows["conflict_detection"] = [low]

    result = _orchestrator(session, rows).scan(user_id="u1")
    assert result.conflicts_found == 1
    assert len(ConflictRepository(session, user_id="u1").list_by_user("u1")) == 1
    assert _push_jobs(session) == []


def test_quiet_user_gets_no_conflict_push(session):
    """免打扰是「先于发送」的闸门：冲突已经入库，只是不通知。"""
    session.add(User(id="u1", username="u1", password_hash="x", push_frequency="quiet"))
    session.commit()
    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [dict(CONFLICT_ROW)]

    result = _orchestrator(session, rows).scan(user_id="u1")
    assert result.conflicts_found == 1
    assert _push_jobs(session) == []


def test_rescan_does_not_duplicate_notification(session):
    """重扫幂等：同一对不重判 → 不产生第二条通知。"""
    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [dict(CONFLICT_ROW)]
    orch = _orchestrator(session, rows)

    orch.scan(user_id="u1")
    assert len(_push_jobs(session)) == 1

    second = orch.scan(user_id="u1")
    assert second.conflicts_found == 0
    assert len(_push_jobs(session)) == 1


def test_switch_off_disables_conflict_push(session, monkeypatch):
    monkeypatch.setattr(settings, "l2_conflict_push_enabled", False)
    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [dict(CONFLICT_ROW)]

    result = _orchestrator(session, rows).scan(user_id="u1")
    assert result.conflicts_found == 1
    assert _push_jobs(session) == []


def test_rejudge_does_not_notify(session):
    """用户显式翻案时他本人就在看，再推一条「发现冲突」是自说自话。

    构造：首轮判「无关」（不入库、无通知）→ 用户翻案判成矛盾 → 冲突入库但**不推**。
    """
    from app.domain.models.l2_judgment_log import L2JudgmentLog

    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [
        {"relation": "无关", "conflict_type": "", "detail": "说的不是一回事",
         "suggestion": "", "confidence": 0.9},
        dict(CONFLICT_ROW),
    ]
    orch = _orchestrator(session, rows)
    first = orch.scan(user_id="u1")
    assert first.conflicts_found == 0
    assert _push_jobs(session) == []

    pair_key = session.scalars(select(L2JudgmentLog)).first().pair_key
    outcome = orch.rejudge_pair(user_id="u1", pair_key=pair_key)
    assert outcome is not None and outcome.conflict_active is True
    assert len(ConflictRepository(session, user_id="u1").list_by_user("u1")) == 1
    assert _push_jobs(session) == []


# ---------- 旁路保证 ----------


def test_push_failure_does_not_break_scan(session, monkeypatch):
    """推送是旁路：入队抛异常只能记日志，扫描结果必须完好（ADR-14）。"""
    from app.agent.push_service import PushService

    def boom(self, **kwargs):
        raise RuntimeError("push channel down")

    monkeypatch.setattr(PushService, "enqueue", boom)
    rows = _two_same_topic_items(session)
    rows["conflict_detection"] = [dict(CONFLICT_ROW)]

    result = _orchestrator(session, rows).scan(user_id="u1")
    assert result.conflicts_found == 1
    assert len(ConflictRepository(session, user_id="u1").list_by_user("u1")) == 1
