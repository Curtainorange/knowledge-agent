"""L5 归因诊断测试（UC-L5-01 健康报告 / UC-L5-02 归因诊断）。

重点：
1. **信号本地算、模型只归因**（与 L4 同一条原则）——行为指标断言不依赖 FakeProvider 措辞；
2. **置信度校准（ADR-15）**：数据充分度缩放 + 信号一致性微调，不再信任模型裸 confidence；
3. 空知识库 / 无行为直接返回 empty，不白花一次模型调用。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agent.l5_orchestrator import L5Orchestrator, calibrate_confidence
from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.models.learning_event import LearningEvent
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
            text = json.dumps({"pattern": "", "root_cause": "", "suggested_action": "",
                               "confidence": 0.5, "reasoning_chain": []})
        return Completion(text=text, prompt_tokens=1, completion_tokens=1, model=model, reasoning=reasoning)


def _orch(session, rows=None, provider=None) -> L5Orchestrator:
    return L5Orchestrator(ModelGateway(provider=provider or FakeProvider(rows or {})), session)


def _add_item(session, user_id: str, title: str = "条目", read_progress: float = 0.0) -> None:
    item = KnowledgeItem(
        user_id=user_id, title=title, raw_content="正文", source="manual",
        read_progress=read_progress, embed_status="embedded",
    )
    session.add(item)
    session.commit()


DIAG_ROWS = {
    "causal_reasoning": [{
        "pattern": "高收藏低完成",
        "root_cause": "24 条里只有 3 条读完，完成比 12%",
        "suggested_action": "每天只读一篇，读完立刻写一句笔记",
        "confidence": 0.8,
        "reasoning_chain": ["收藏量大完成率低", "活跃度下降", "病根是收藏即满足"],
    }]
}


# ---------- 置信度校准（ADR-15）----------


def test_calibrate_scales_by_data_support():
    # 行为样本极少：模型 0.9 也要打折
    low = calibrate_confidence(0.9, event_count=5, signal_count=1)
    high = calibrate_confidence(0.9, event_count=50, signal_count=1)
    assert low < high
    assert low < 0.5  # 数据不足，即便模型自信也不该高置信
    # 一致性微调：多信号略高于单信号
    one = calibrate_confidence(0.8, event_count=50, signal_count=1)
    many = calibrate_confidence(0.8, event_count=50, signal_count=3)
    assert many > one


def test_calibrate_bounds():
    assert 0.0 <= calibrate_confidence(1.0, event_count=1000, signal_count=10) <= 1.0
    assert calibrate_confidence(0.0, event_count=0, signal_count=0) >= 0.0


# ---------- 归因诊断 ----------


def test_diagnose_ok(session):
    for i in range(5):
        _add_item(session, "u1", title=f"条目{i}")
    provider = FakeProvider(DIAG_ROWS)
    orch = _orch(session, provider=provider)
    result = orch.diagnose(user_id="u1")

    assert result.state == "ok"
    assert result.pattern == "高收藏低完成"
    assert result.root_cause
    assert result.suggested_action
    assert result.diagnosis_id
    assert "causal_reasoning" in provider.calls  # 归因这一步才调模型

    row = session.scalars(select(CognitiveDiagnosis)).first()
    assert row is not None and row.status == "pending"
    assert row.confidence > 0  # 校准后的置信度已落库

    types = {e.event_type for e in session.scalars(select(LearningEvent))}
    assert events.L5_DIAGNOSIS_CREATED in types


def test_diagnose_empty_when_no_activity(session):
    result = _orch(session).diagnose(user_id="u1")
    assert result.state == "empty"
    assert result.diagnosis_id == ""


def test_diagnose_degrades_on_bad_json(session):
    for i in range(5):
        _add_item(session, "u1", title=f"条目{i}")
    orch = _orch(session, {"causal_reasoning": ["坏 JSON {"]})
    result = orch.diagnose(user_id="u1")
    assert result.state == "degraded"
    assert "归因分析失败" in result.note


# ---------- 用户决定 ----------


def test_decide_accept_and_reject(session):
    for i in range(5):
        _add_item(session, "u1", title=f"条目{i}")
    orch = _orch(session, DIAG_ROWS)
    result = orch.diagnose(user_id="u1")

    ok, message = orch.decide(user_id="u1", diagnosis_id=result.diagnosis_id, accepted=True)
    assert ok and "采纳" in message
    row = session.scalars(select(CognitiveDiagnosis)).first()
    assert row.status == "accepted"


def test_decide_unknown_diagnosis(session):
    ok, message = _orch(session).decide(user_id="u1", diagnosis_id="不存在", accepted=True)
    assert ok is False
    assert "不存在" in message


# ---------- API ----------


def test_api_diagnosis_flow(client):
    from app.api import deps
    from app.main import app

    fake = FakeProvider(DIAG_ROWS)
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=fake)
    try:
        headers = auth_headers(client, "l5_api_user")
        # 空知识库 → empty，不调模型
        empty = client.post("/api/v1/l5/diagnosis", headers=headers)
        assert empty.status_code == 200
        assert empty.json()["state"] == "empty"

        # 造几条知识后 → ok
        for i in range(5):
            client.post(
                "/api/v1/knowledge/items",
                json={"title": f"条目{i}", "content": "正文内容", "source": "manual"},
                headers=headers,
            )
        resp = client.post("/api/v1/l5/diagnosis", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["state"] == "ok"
        assert body["pattern"] and body["root_cause"]

        latest = client.get("/api/v1/l5/diagnosis/latest", headers=headers).json()
        assert latest["diagnosis_id"] == body["diagnosis_id"]

        decided = client.post(
            f"/api/v1/l5/diagnosis/{body['diagnosis_id']}/decision",
            json={"accepted": True}, headers=headers,
        )
        assert decided.status_code == 200
        assert "采纳" in decided.json()["message"]
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)


def test_api_l5_requires_auth(client):
    assert client.post("/api/v1/l5/diagnosis").status_code == 401
    assert client.get("/api/v1/l5/diagnosis/latest").status_code == 401
