"""排期参数可改（F 模块的「可改」）。

此前的状态：`PlanParams` 硬编码在 `DEFAULT_PARAMS`，计划页只把
「每周 300 分钟」印出来，没有任何入口能改。后果是结构性的——
一个每周只有两小时的人，拿着「每周 5 小时」的容量，计划从第一天就排不完。

这里钉四件事：

- **读回来的是生效值**：没设过就返回默认，而不是让前端自己补一套默认；
- **写入必收敛**：越界夹紧、类型错误丢弃、缺项补默认，永远返回能用的对象；
- **部分更新不误伤**：只改「每周分钟」时，其他三项保持原值（老前端只传
  `push_frequency` 时也不能把排期参数清掉）；
- **参数真的生效**：改完生成计划，`rules_snapshot` 与单周容量都要跟着变。
"""
from __future__ import annotations

import json

import pytest

from app.agent import plan_rules
from app.agent.plan_rules import PlanParams, from_prefs, to_prefs
from app.api import deps
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.helpers import sign_in


# ---------- from_prefs：脏数据也要能变成能用的参数 ----------


def test_none_means_default():
    assert from_prefs(None) == plan_rules.DEFAULT_PARAMS
    assert from_prefs({}) == plan_rules.DEFAULT_PARAMS
    assert from_prefs("不是字典") == plan_rules.DEFAULT_PARAMS


def test_out_of_range_is_clamped_not_rejected():
    params = from_prefs({"weekly_minutes": 0, "new_ratio": 5, "min_task_minutes": -3})
    assert params.weekly_minutes == plan_rules.PARAM_BOUNDS["weekly_minutes"][0]
    assert params.new_ratio == plan_rules.PARAM_BOUNDS["new_ratio"][1]
    assert params.min_task_minutes == plan_rules.PARAM_BOUNDS["min_task_minutes"][0]

    big = from_prefs({"weekly_minutes": 999999})
    assert big.weekly_minutes == plan_rules.PARAM_BOUNDS["weekly_minutes"][1]


def test_bad_types_fall_back_to_default_fields():
    params = from_prefs({"weekly_minutes": "很多", "new_ratio": None})
    assert params.weekly_minutes == plan_rules.DEFAULT_PARAMS.weekly_minutes
    assert params.new_ratio == plan_rules.DEFAULT_PARAMS.new_ratio


def test_review_weeks_cleaned_and_bounded():
    params = from_prefs({"review_weeks": [4, 2, 2, 99, 0, "三"]})
    assert list(params.review_weeks) == [2, 4]     # 去重、排序、丢掉非 1–52 的值


def test_review_weeks_never_empty_and_capped():
    assert list(from_prefs({"review_weeks": []}).review_weeks) == list(
        plan_rules.DEFAULT_REVIEW_WEEKS
    )
    capped = from_prefs({"review_weeks": [1, 2, 3, 4, 5]})
    assert len(capped.review_weeks) == plan_rules.MAX_REVIEW_WEEKS


def test_min_bigger_than_max_raises_max():
    """用户说「至少 45 分钟」比「最多 15 分钟」更可能是真实意图。"""
    params = from_prefs({"min_task_minutes": 45, "max_task_minutes": 15})
    assert params.max_task_minutes == 45


def test_to_prefs_has_no_rule_version():
    payload = to_prefs(PlanParams(weekly_minutes=90))
    assert "version" not in payload          # 规则版本是快照的事，不是用户的设置
    assert payload["weekly_minutes"] == 90
    assert isinstance(payload["review_weeks"], list)


# ---------- 接口：读回来的永远是生效值 ----------


def test_get_returns_default_when_never_set(client):
    headers = sign_in(client, "param_get").headers
    data = client.get("/api/v1/preferences", headers=headers).json()
    assert data["plan_params"]["weekly_minutes"] == plan_rules.DEFAULT_PARAMS.weekly_minutes
    assert data["plan_params"]["review_weeks"] == list(plan_rules.DEFAULT_PARAMS.review_weeks)


def test_partial_update_keeps_other_fields(client):
    headers = sign_in(client, "param_partial").headers
    client.put(
        "/api/v1/preferences",
        json={"plan_params": {"weekly_minutes": 120, "new_ratio": 0.6}},
        headers=headers,
    )
    data = client.put(
        "/api/v1/preferences",
        json={"plan_params": {"weekly_minutes": 90}},
        headers=headers,
    ).json()
    assert data["plan_params"]["weekly_minutes"] == 90
    assert data["plan_params"]["new_ratio"] == 0.6          # 没传的项不能被重置
    assert data["plan_params"]["review_weeks"] == list(plan_rules.DEFAULT_PARAMS.review_weeks)


def test_empty_object_restores_default(client):
    headers = sign_in(client, "param_reset").headers
    client.put(
        "/api/v1/preferences", json={"plan_params": {"weekly_minutes": 60}}, headers=headers
    )
    data = client.put("/api/v1/preferences", json={"plan_params": {}}, headers=headers).json()
    assert data["plan_params"]["weekly_minutes"] == plan_rules.DEFAULT_PARAMS.weekly_minutes


def test_out_of_range_is_stored_clamped(client):
    headers = sign_in(client, "param_clamp").headers
    data = client.put(
        "/api/v1/preferences", json={"plan_params": {"weekly_minutes": 1}}, headers=headers
    ).json()
    # 存的是夹紧后的值：库里不该留下「每周 1 分钟」这种排不出东西的设置
    assert data["plan_params"]["weekly_minutes"] == plan_rules.PARAM_BOUNDS["weekly_minutes"][0]


def test_push_only_update_does_not_touch_plan_params(client):
    """老前端（notify.html）只传 push_frequency，不能把排期参数清掉。"""
    headers = sign_in(client, "param_push_only").headers
    client.put(
        "/api/v1/preferences", json={"plan_params": {"weekly_minutes": 150}}, headers=headers
    )
    data = client.put(
        "/api/v1/preferences", json={"push_frequency": "quiet"}, headers=headers
    ).json()
    assert data["push_frequency"] == "quiet"
    assert data["plan_params"]["weekly_minutes"] == 150


# ---------- 参数真的生效 ----------


class _PlanProvider(LLMProvider):
    def __init__(self, rows: list[str]) -> None:
        self.rows = list(rows)

    def chat(
        self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
    ) -> Completion:
        text = self.rows.pop(0) if self.rows else json.dumps({"tasks": [], "rationale": ""})
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model=model, reasoning=reasoning,
        )


def _plan_payload(weeks: int = 4) -> str:
    return json.dumps({
        "tasks": [
            {"week_index": i, "subject": f"第 {i} 周：读透一个概念", "focus": "", "related_item_ids": []}
            for i in range(1, weeks + 1)
        ],
        "rationale": "每周一个主题",
    }, ensure_ascii=False)


def test_user_capacity_reaches_the_plan(client):
    """改完参数生成计划：快照里的容量与单周时长都要跟着用户的值走。"""
    provider = _PlanProvider([_plan_payload(4)])
    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=provider)
    try:
        headers = sign_in(client, "param_effective").headers
        client.put(
            "/api/v1/preferences",
            json={"plan_params": {
                "weekly_minutes": 60, "new_ratio": 0.5, "review_weeks": [2],
                "min_task_minutes": 15, "max_task_minutes": 60,
            }},
            headers=headers,
        )
        goal = client.post(
            "/api/v1/l4/goals", json={"description": "两个月读透一本书", "priority": "high"},
            headers=headers,
        ).json()
        plan = client.post(f"/api/v1/l4/goals/{goal['goal_id']}/plan", headers=headers)
        assert plan.status_code == 200, plan.text
        body = plan.json()
        assert body["rules_snapshot"]["weekly_minutes"] == 60
        assert body["rules_snapshot"]["new_ratio"] == 0.5
        assert body["rules_snapshot"]["review_weeks"] == [2]

        weekly: dict[int, int] = {}
        for task in body["tasks"]:
            week = task["week_index"]
            weekly[week] = weekly.get(week, 0) + task["minutes"]
        over = {w: m for w, m in weekly.items() if m > 60}
        assert not over, f"单周超过用户设定的 60 分钟：{over}"
    finally:
        app.dependency_overrides.pop(deps.get_gateway, None)


def test_halved_capacity_shrinks_total_minutes():
    """AT-F3：把周容量减半重排，总时长应明显下降（而不是原地不动）。"""
    tasks = [{"week_index": 1, "subject": f"主题{i}", "focus": ""} for i in range(6)]
    wide, _ = plan_rules.normalize(tasks, params=PlanParams(weekly_minutes=600))
    narrow, _ = plan_rules.normalize(tasks, params=PlanParams(weekly_minutes=120))
    wide_total = sum(t.minutes for t in wide)
    narrow_total = sum(t.minutes for t in narrow)
    # 任务内容不变，但每周能给的时间少了 5 倍——总时长必须跟着降
    assert narrow_total < wide_total * 0.5
