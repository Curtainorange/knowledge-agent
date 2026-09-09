"""网关单元测试：策略映射、Mock 确定性、成本埋点。全程不触发真实模型。"""
from __future__ import annotations

from fastapi import Depends

from app.core import trace
from app.domain.models.cost_log import CostLog
from app.llm.gateway import ModelGateway, strategy_for, Strategy


def test_strategy_mapping():
    assert strategy_for("deep_reasoning").reasoning is True
    assert strategy_for("batch_extraction").reasoning is False
    assert strategy_for("multi_turn_dialogue").reasoning is True
    assert strategy_for("unknown_task") == strategy_for("default")
    assert strategy_for("unknown_task").reasoning is False


def test_gateway_routes_task_to_reasoning():
    gw = ModelGateway()  # 无 KEY → MockProvider
    strat = gw.route("conflict_detection")
    assert isinstance(strat, Strategy)
    assert strat.reasoning is True
    assert strat.model  # 回落 settings.deepseek_model，非空


def test_gateway_chat_deterministic_and_billable(session):
    gw = ModelGateway()
    with trace.request_id("req-test-gateway"):
        c1 = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "你好"}], session=session)
        c2 = gw.chat(task_type="multi_turn_dialogue", messages=[{"role": "user", "content": "你好"}], session=session)
    # MockProvider 确定性：相同输入 → 相同输出与 token
    assert c1.text == c2.text
    assert c1.reasoning is True
    assert c1.prompt_tokens > 0

    # 成本落库（以本测试 request_id 过滤，避免跨测试共享内存库污染）
    session.flush()
    rows = session.query(CostLog).filter(CostLog.request_id == "req-test-gateway").all()
    assert len(rows) == 2
    assert all(r.reasoning is True for r in rows)
    assert all(r.estimated_cost >= 0 for r in rows)