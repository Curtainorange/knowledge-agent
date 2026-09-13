"""计费测试：分时段单价 + 缓存命中/未命中分账（这两项决定成本账是否失真一个量级）。

不触网、不调模型：直接测估算函数与 CostLog 落库，用 monkeypatch 固定单价。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.domain.models.cost_log import CostLog
from app.llm import cost as cost_module
from app.llm.completion import Completion
from app.llm.cost import _estimate_cost, is_peak_time, record_cost

CST = timezone(timedelta(hours=8))


@pytest.fixture()
def fixed_prices(monkeypatch):
    """固定单价便于断言：空闲 输入 1 元 / 命中 0.02 元 / 输出 4 元，高峰 ×2。"""
    monkeypatch.setattr(settings, "deepseek_price_input_per_1m", 1.0)
    monkeypatch.setattr(settings, "deepseek_price_cache_hit_per_1m", 0.02)
    monkeypatch.setattr(settings, "deepseek_price_output_per_1m", 4.0)
    monkeypatch.setattr(settings, "deepseek_peak_multiplier", 2.0)
    monkeypatch.setattr(settings, "deepseek_peak_hours", "9-12,14-18")


# ---------- 高峰时段判定 ----------


def test_peak_windows_on_weekdays(fixed_prices):
    monday = datetime(2026, 9, 14, tzinfo=CST)  # 2026-09-14 是周一
    assert is_peak_time(monday.replace(hour=10)) is True
    assert is_peak_time(monday.replace(hour=14, minute=30)) is True
    assert is_peak_time(monday.replace(hour=8, minute=59)) is False
    assert is_peak_time(monday.replace(hour=12, minute=0)) is False  # 12 点起是午休空闲
    assert is_peak_time(monday.replace(hour=13, minute=59)) is False
    assert is_peak_time(monday.replace(hour=18, minute=0)) is False  # 18 点起空闲（右开区间）
    assert is_peak_time(monday.replace(hour=20)) is False


def test_weekend_is_always_off_peak(fixed_prices):
    saturday = datetime(2026, 9, 19, 10, 0, tzinfo=CST)
    assert is_peak_time(saturday) is False


def test_peak_hours_config_is_parsed_leniently(fixed_prices, monkeypatch):
    """配置串非法片段忽略、合法片段生效——不因一个笔误把计费整体带偏。"""
    monkeypatch.setattr(settings, "deepseek_peak_hours", "9-12, broken, ,18-20")
    monday = datetime(2026, 9, 14, tzinfo=CST)
    assert is_peak_time(monday.replace(hour=10)) is True
    assert is_peak_time(monday.replace(hour=19)) is True
    assert is_peak_time(monday.replace(hour=14)) is False


# ---------- 缓存命中分账 ----------


def test_cached_tokens_billed_at_cache_hit_price(fixed_prices, monkeypatch):
    monkeypatch.setattr(cost_module, "is_peak_time", lambda now=None: False)

    # 1000 输入全未命中 + 100 输出：1000×1 + 100×4 = 1400（每百万 token 单价）
    assert _estimate_cost(1000, 100, cached_tokens=0) == pytest.approx(0.0014)
    # 800 未命中 + 200 命中：800×1 + 200×0.02 + 100×4 = 1204
    assert _estimate_cost(1000, 100, cached_tokens=200) == pytest.approx(0.001204)
    # 命中越多越便宜，且不会出现负成本
    assert _estimate_cost(1000, 100, cached_tokens=5000) < _estimate_cost(1000, 100, cached_tokens=0)


def test_peak_time_doubles_cost(fixed_prices, monkeypatch):
    monkeypatch.setattr(cost_module, "is_peak_time", lambda now=None: False)
    off_peak = _estimate_cost(1000, 100, cached_tokens=0)
    monkeypatch.setattr(cost_module, "is_peak_time", lambda now=None: True)
    peak = _estimate_cost(1000, 100, cached_tokens=0)
    assert peak == pytest.approx(off_peak * 2)


# ---------- 落库 ----------


def test_record_cost_persists_cached_tokens(session, fixed_prices, monkeypatch):
    monkeypatch.setattr(cost_module, "is_peak_time", lambda now=None: False)
    completion = Completion(
        text="ok", prompt_tokens=1000, completion_tokens=100, cached_tokens=400,
        model="deepseek-flash", reasoning=False,
    )
    record_cost(
        session, user_id="u1", task_type="l1_mining",
        model="deepseek-flash", reasoning=True, completion=completion,
    )
    session.flush()

    row = session.scalars(select(CostLog)).first()
    assert row is not None
    assert row.cached_tokens == 400
    assert row.prompt_tokens == 1000
    # 600×1 + 400×0.02 + 100×4 = 1008（每百万）
    assert row.estimated_cost == pytest.approx(0.001008)


# ---------- provider 解析缓存字段 ----------


def test_provider_reads_cached_tokens_from_either_field():
    from app.llm.deepseek.provider import DeepSeekProvider

    class Details:
        cached_tokens = 321

    class UsageOpenAI:
        prompt_tokens_details = Details()

    class UsageDeepSeek:
        prompt_tokens_details = None
        prompt_cache_hit_tokens = 654

    class UsageNone:
        prompt_tokens_details = None

    assert DeepSeekProvider._cached_tokens(UsageOpenAI()) == 321
    assert DeepSeekProvider._cached_tokens(UsageDeepSeek()) == 654
    assert DeepSeekProvider._cached_tokens(UsageNone()) == 0
    assert DeepSeekProvider._cached_tokens(None) == 0