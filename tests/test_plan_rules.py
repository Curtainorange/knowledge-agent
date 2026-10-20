"""F 模块测试：计划规则引擎（周容量 / 新复比 / 复习点 / 剩余进度）。

对应 `docs/修改方案-2026-10-20.md` F 模块验收标准 AT-F1~F6。
全是纯函数，不碰数据库、不调模型。
"""
from __future__ import annotations

from app.agent import plan_rules
from app.agent.plan_rules import PlanParams


def _tasks(*weeks: int) -> list[dict]:
    """每周一个任务的最小输入（GeneratedTask.model_dump 形状）。"""
    return [
        {"week_index": w, "subject": f"第 {w} 周任务", "focus": "", "related_item_ids": []}
        for w in weeks
    ]


PARAMS = PlanParams(weekly_minutes=120, new_ratio=0.5, review_weeks=(2,))


# ---- 周容量 -----------------------------------------------------------------


def test_each_week_fits_capacity():
    tasks, _ = plan_rules.normalize(_tasks(1, 2, 3), params=PARAMS)
    for week in (1, 2, 3):
        total = sum(t.minutes for t in tasks if t.week_index == week and t.task_type == "new")
        assert total <= PARAMS.weekly_minutes


def test_minutes_split_evenly():
    tasks, _ = plan_rules.normalize(
        [
            {"week_index": 1, "subject": "甲"},
            {"week_index": 1, "subject": "乙"},
        ],
        params=PARAMS,
    )
    minutes = [t.minutes for t in tasks if t.task_type == "new"]
    assert len(set(minutes)) == 1


def test_overflow_is_deferred_not_dropped():
    """装不下的任务顺延到下周，**不丢**。

    丢了用户根本不知道自己少学了什么；顺延至少看得见。
    """
    many = [{"week_index": 1, "subject": f"任务 {i}"} for i in range(12)]
    tasks, snapshot = plan_rules.normalize(many, params=PARAMS)
    assert snapshot["deferred"] > 0
    assert snapshot["notes"], "顺延必须有说明，不能悄悄改了计划"
    # 一条都没丢：12 条新学任务全在（另有一条规则插入的复习任务）
    assert len([t for t in tasks if t.task_type == "new"]) == 12


def test_deferred_task_carries_reason():
    many = [{"week_index": 1, "subject": f"任务 {i}"} for i in range(12)]
    tasks, _ = plan_rules.normalize(many, params=PARAMS)
    deferred = [t for t in tasks if t.basis.get("params", {}).get("deferred_from")]
    assert deferred
    assert "顺延" in deferred[0].basis["why"]


# ---- 新复比与复习点 ---------------------------------------------------------


def test_review_inserted_on_configured_week():
    tasks, snapshot = plan_rules.normalize(_tasks(1, 2), params=PARAMS)
    reviews = [t for t in tasks if t.task_type == "review"]
    assert reviews
    assert reviews[0].week_index == 2
    assert "回看" in reviews[0].subject
    assert snapshot["review_count"] == len(reviews)


def test_no_review_on_first_week():
    """第 1 周没有可回看的内容，不该硬插一条。"""
    tasks, _ = plan_rules.normalize(_tasks(1), params=PARAMS)
    assert not [t for t in tasks if t.task_type == "review"]


def test_review_points_at_previous_week():
    tasks, _ = plan_rules.normalize(_tasks(1, 2, 3), params=PARAMS)
    review = next(t for t in tasks if t.task_type == "review")
    assert review.basis["params"]["source_week"] == 1


def test_review_leaves_room_for_new_learning():
    """有复习的那周，新学预算按 new_ratio 收缩，给回看留时间。"""
    tasks, _ = plan_rules.normalize(_tasks(1, 2), params=PARAMS)
    week2_new = sum(
        t.minutes for t in tasks if t.week_index == 2 and t.task_type == "new"
    )
    assert week2_new <= PARAMS.weekly_minutes * PARAMS.new_ratio


# ---- 剩余进度 ---------------------------------------------------------------


def test_remaining_ratio_skips_earlier_tasks():
    tasks, snapshot = plan_rules.normalize(_tasks(1, 2, 3, 4), params=PARAMS, remaining_ratio=0.5)
    assert snapshot["remaining_ratio"] == 0.5
    # 前一半被跳过：第 1 周的任务不该出现
    assert not [t for t in tasks if t.week_index == 1]
    assert any(t.week_index >= 3 for t in tasks)


def test_remaining_ratio_records_why_in_first_task():
    tasks, _ = plan_rules.normalize(_tasks(1, 2, 3, 4), params=PARAMS, remaining_ratio=0.5)
    first = tasks[0]
    note = (first.basis or {}).get("progress_note")
    assert note and note["rule"] == "remaining_progress"
    assert "跳过" in note["why"] or "没有排进来" in note["why"]


def test_remaining_ratio_keeps_at_least_one_task():
    tasks, _ = plan_rules.normalize(_tasks(1, 2), params=PARAMS, remaining_ratio=0.1)
    assert tasks, "不能因为进度适配把计划排空"


def test_full_remaining_keeps_everything():
    tasks, _ = plan_rules.normalize(_tasks(1, 2), params=PARAMS, remaining_ratio=1.0)
    assert [t.week_index for t in tasks if t.task_type == "new"] == [1, 2]


# ---- 依据与快照 -------------------------------------------------------------


def test_every_task_has_a_readable_reason():
    """「为什么排在这里」必须每条都有——没有理由的排法无法被质疑。"""
    tasks, _ = plan_rules.normalize(_tasks(1, 2, 3), params=PARAMS, remaining_ratio=0.8)
    for task in tasks:
        basis = task.basis or {}
        assert basis.get("why"), task.subject
        assert basis.get("rule"), task.subject


def test_snapshot_carries_params_for_traceability():
    _, snapshot = plan_rules.normalize(_tasks(1, 2), params=PARAMS)
    assert snapshot["version"] == plan_rules.RULES_VERSION
    assert snapshot["weekly_minutes"] == 120
    assert snapshot["new_ratio"] == 0.5
    assert snapshot["review_weeks"] == [2]
    assert snapshot["task_count"] >= 2


def test_tasks_sorted_by_week_then_new_first():
    tasks, _ = plan_rules.normalize(_tasks(1, 2), params=PARAMS)
    weeks = [t.week_index for t in tasks]
    assert weeks == sorted(weeks)
    # 同一周内新学排在复习前面
    week2 = [t.task_type for t in tasks if t.week_index == 2]
    assert week2 == sorted(week2, key=lambda x: 0 if x == "new" else 1)


def test_empty_input_yields_empty_snapshot():
    tasks, snapshot = plan_rules.normalize([], params=PARAMS)
    assert tasks == []
    assert snapshot["task_count"] == 0


def test_blank_subject_dropped():
    tasks, _ = plan_rules.normalize(
        [{"week_index": 1, "subject": "  "}, {"week_index": 1, "subject": "真任务"}],
        params=PARAMS,
    )
    assert [t.subject for t in tasks if t.task_type == "new"] == ["真任务"]


def test_describe_snapshot_is_readable():
    _, snapshot = plan_rules.normalize(_tasks(1, 2), params=PARAMS)
    text = plan_rules.describe_snapshot(snapshot)
    assert "120 分钟" in text
    assert "50%" in text


def test_round_minutes_snaps_to_five():
    assert plan_rules.round_minutes(37) == 40
    assert plan_rules.round_minutes(35) == 35
