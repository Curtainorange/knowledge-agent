"""主动学习教练测试（第二阶段 C4）：L3/L4/L5 建议纯本地聚合为周教练提示。

重点：
1. 聚合零 LLM——collect + assemble 纯本地，唯一模型成本是 L3 brief（空库零调用）；
2. 空材料不打扰——assemble 返回 None 就不推；
3. 防鸡汤化——每块必须带「下一步：」，说不出下一步的内容不上推送；
4. ISO 周幂等——同周 ensure 只排一次（coach:weekly 键分桶）。
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from app.agent.coach import CoachBlock, CoachMaterials, assemble, collect_materials
from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis
from app.domain.models.push_job import PushJob
from app.domain.models.user import User
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.feedback import events
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.workers import handlers as _handlers  # noqa: F401  导入即注册
from app.workers.handlers import coach_weekly
from app.workers.triggers import coach_weekly_key, ensure_coach_schedules


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
            text = json.dumps({"assignments": [], "patterns": [], "questions": [], "overview": ""})
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning)


def _patch_gateway(monkeypatch, provider: FakeProvider) -> FakeProvider:
    real = ModelGateway

    def _factory(*args, **kwargs):
        return real(provider=provider)

    monkeypatch.setattr("app.llm.gateway.ModelGateway", _factory)
    return provider


def _add_diagnosis(session, pattern: str = "高收藏低完成", status: str = "pending") -> CognitiveDiagnosis:
    diag = CognitiveDiagnosis(
        user_id="u1", pattern=pattern, root_cause="收藏即满足",
        confidence=0.7, suggested_action="每收藏 3 条先读完 1 条",
        reasoning_chain="r", status=status,
    )
    session.add(diag)
    session.commit()
    return diag


def _seed_knowledge(session) -> list[str]:
    repo = KnowledgeRepository(session, user_id="u1")
    ids = [repo.create(user_id="u1", title=t, content=f"{t} 的正文").id for t in ("如何开始健身", "健身平台期突破")]
    session.commit()
    return ids


# ---------- 幂等键分桶 ----------


def test_coach_keys_week_bucketed():
    mon = datetime(2026, 9, 28)   # ISO 2026-W40 周一
    sun = datetime(2026, 10, 4)   # 同周周日
    nxt = datetime(2026, 10, 5)   # 下周一
    assert coach_weekly_key("u1", mon) == coach_weekly_key("u1", sun)
    assert coach_weekly_key("u1", mon) != coach_weekly_key("u1", nxt)
    assert coach_weekly_key("u1", mon) == "coach:weekly:u1:2026-W40"


def test_ensure_coach_queues_once_per_week(session):
    assert ensure_coach_schedules(session, ["u1"]) == 1
    assert ensure_coach_schedules(session, ["u1"]) == 0  # 同周 ensure 不再新建


# ---------- 聚合：纯本地、防鸡汤 ----------


def test_collect_materials_priority_pending_diagnosis(session):
    """pending 诊断优先成块（结论 + 下一步），已拒绝的不进。"""
    _add_diagnosis(session, pattern="高收藏低完成", status="pending")
    _add_diagnosis(session, pattern="已拒绝的", status="rejected")

    materials = collect_materials(session, "u1")

    labels = [b.label for b in materials.blocks]
    assert labels.count("待决定建议") == 1
    block = materials.blocks[0]
    assert "高收藏低完成" in block.summary
    assert block.next_step.strip()  # 每块必须有下一步


def test_collect_materials_deviation_event_block(session):
    """本周偏离信号进块（只引事件元数据标签，不搬归因正文）。"""
    LearningEventRepository(session).append(
        user_id="u1", event_type=events.L4_DEVIATION_CHECKED,
        payload={"reasons": ["idle_days=5", "完成度 0/3"], "root_cause": "不该出现的归因正文"},
    )
    session.commit()

    materials = collect_materials(session, "u1")

    dev = [b for b in materials.blocks if b.label == "偏离信号"]
    assert len(dev) == 1
    assert "idle_days=5" in dev[0].summary
    assert "归因正文" not in dev[0].summary  # 事件只引元数据


def test_assemble_renders_next_steps_and_drops_soup(session):
    """防鸡汤：正文每块含「下一步：」；说不出下一步的块被丢弃，全丢完返回 None。"""
    digest = assemble(CoachMaterials(blocks=[
        CoachBlock(label="待决定建议", summary="高收藏低完成", next_step="在通知页决定"),
        CoachBlock(label="目标进度", summary="1/2 已完成", next_step=""),
    ]), [])

    assert digest is not None
    assert "下一步：在通知页决定" in digest.body
    assert "目标进度" not in digest.body  # 无下一步的块不上推送

    assert assemble(CoachMaterials(blocks=[CoachBlock(label="x", summary="y", next_step="")]), []) is None


def test_assemble_caps_blocks_and_questions():
    blocks = [CoachBlock(label=f"块{i}", summary=f"s{i}", next_step=f"n{i}") for i in range(6)]

    class Q:
        def __init__(self, i):
            self.question = f"问{i}？"
            self.next_step = f"追问动作{i}"

    digest = assemble(CoachMaterials(blocks=blocks), [Q(i) for i in range(4)], max_items=3)

    assert digest is not None
    assert sum(1 for line in digest.body.splitlines() if line.startswith("【块")) == 3  # 块上限
    assert sum(1 for line in digest.body.splitlines() if line.startswith("· 问")) == 2  # 追问上限 2


def test_assemble_week_label_injectable():
    """周标签可注入 now——测试能稳定断言 ISO 周格式。"""
    digest = assemble(
        CoachMaterials(blocks=[CoachBlock(label="x", summary="s", next_step="n")]),
        [], now=datetime(2026, 9, 28),  # ISO 2026-W40 周一
    )
    assert digest.week_label == "2026-W40"
    assert "2026-W40" in digest.title


def test_eval_line_suppressed_on_zero_labels(session):
    """弱真值 0/0 没有信息量，不拼「采纳 0 / 忽略 0」噪音。"""
    LearningEventRepository(session).append(
        user_id="u1", event_type=events.L2_EVAL_WEEKLY,
        payload={"n_accepted": 0, "n_ignored": 0, "precision": 0.0},
    )
    session.commit()
    _add_diagnosis(session)

    materials = collect_materials(session, "u1")

    assert materials.eval_line == ""
    assert any(b.label == "待决定建议" for b in materials.blocks)  # 诊断块不受影响


def test_eval_line_shown_with_weak_label_data(session):
    LearningEventRepository(session).append(
        user_id="u1", event_type=events.L2_EVAL_WEEKLY,
        payload={"n_accepted": 3, "n_ignored": 1, "precision": 0.75},
    )
    session.commit()

    materials = collect_materials(session, "u1")

    assert "采纳 3 / 忽略 1" in materials.eval_line


# ---------- 周 handler 全链路 ----------


def test_weekly_handler_runs_l3_brief_only_and_pushes(session, monkeypatch):
    """聚合零 LLM：全链路 LLM 调用恰好 = L3 brief 的 2 次，追问进正文。"""
    ids = _seed_knowledge(session)
    provider = _patch_gateway(monkeypatch, FakeProvider({
        "topic_analysis": [{"assignments": [
            {"item_id": ids[0], "topic": "健身", "level": "入门"},
            {"item_id": ids[1], "topic": "健身", "level": "进阶"},
        ]}],
        "cognitive_brief": [{
            "patterns": ["大量存在：入门级内容"],
            "questions": [{
                "question": "入门之后的瓶颈是什么？",
                "why": "入门 1、进阶 1",
                "evidence": "健身 2 篇（入门 1、进阶 1）",
                "next_step": "整理一页平台期手册",
            }],
        }],
    }))
    _add_diagnosis(session)

    coach_weekly({"user_id": "u1"}, session)

    assert provider.calls == ["topic_analysis", "cognitive_brief"]  # 聚合零额外调用
    jobs = list(session.scalars(select(PushJob)))
    assert len(jobs) == 1
    assert jobs[0].push_type == "coach"
    assert "教练提示" in jobs[0].title
    assert "【本周追问】" in jobs[0].body
    assert "下一步：整理一页平台期手册" in jobs[0].body  # UC-L3-01 自动送达


def test_weekly_handler_degrades_brief_but_still_pushes(session, monkeypatch):
    """空库 L3 降级 questions=[]，本地材料照常推。"""
    _patch_gateway(monkeypatch, FakeProvider({}))  # 空库 brief 零调用
    _add_diagnosis(session)

    coach_weekly({"user_id": "u1"}, session)

    jobs = list(session.scalars(select(PushJob)))
    assert len(jobs) == 1
    assert "【待决定建议】" in jobs[0].body
    assert "【本周追问】" not in jobs[0].body


def test_weekly_handler_skips_when_no_materials(session, monkeypatch):
    """空材料不打扰：无诊断/无事件/无计划 → 不推。"""
    provider = _patch_gateway(monkeypatch, FakeProvider({}))

    coach_weekly({"user_id": "u1"}, session)

    assert provider.calls == []  # 空库 brief 快路径零调用
    assert list(session.scalars(select(PushJob))) == []


def test_weekly_quiet_suppressed(session, monkeypatch):
    """quiet 免打扰拦教练提示（coach 非重大诊断例外）。"""
    session.add(User(id="u1", username="u_1", password_hash="x", push_frequency="quiet"))
    session.commit()
    _patch_gateway(monkeypatch, FakeProvider({}))
    _add_diagnosis(session)

    coach_weekly({"user_id": "u1"}, session)

    assert list(session.scalars(select(PushJob))) == []
