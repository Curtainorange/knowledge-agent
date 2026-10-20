"""计划规则引擎（F 模块）：模型排任务，代码管约束。

## 为什么规则不能交给模型

模型排计划时会"看起来合理"：每周三个任务、主题连贯、有理有据。
但它不知道你**每周只有 5 小时**，也不知道第 3 周该回看第 1 周的内容。
更要命的是，这些约束即使写进提示词，也无法核查——用户看到一张漂亮的计划，
执行到第二周就崩了，而没人说得清是哪一步错了。

所以分工是：

- **模型负责"学什么"**：把目标拆成有意义的主题序列（这是它擅长的）
- **代码负责"装得下吗、什么时候复习、为什么排在这里"**（这是可以判定的）

## 四条规则

| 规则 | 作用 | 参数 |
|---|---|---|
| 周容量 | 每周总时长不超过可用时间，装不下的顺延 | `weekly_minutes` |
| 新复比 | 每周给复习留出固定比例的时间 | `new_ratio` |
| 复习点 | 在第 2、4 周插入对前面主题的回看 | `review_weeks` |
| 剩余进度 | 书已读一半就不从第一章排 | `remaining_ratio` |

每条规则都会在任务的 `basis` 里留下**可读的理由**——这就是 l4.html 的依据抽屉。
规则说不出理由的地方，就是规则不应该存在的地方。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# 参数版本：改了默认值或算法就 bump，快照里会记下来，便于回溯"当时为什么这么排"
RULES_VERSION = "v1"

# 任务类型
TYPE_NEW = "new"
TYPE_REVIEW = "review"

_TYPE_LABELS = {TYPE_NEW: "新学", TYPE_REVIEW: "复习"}


@dataclass(frozen=True)
class PlanParams:
    """排期参数。**默认值是可解释的常识值，不是精确测量**——用户可以在设置里改。"""

    weekly_minutes: int = 300      # 每周可用 5 小时
    new_ratio: float = 0.7         # 每周 70% 时间学新的、30% 回看
    review_weeks: tuple[int, ...] = (2, 4)  # 在第 2、4 周插复习（间隔重复的常见节奏）
    min_task_minutes: int = 15     # 再少就不成块，碎片时间做不了
    max_task_minutes: int = 90     # 一次超过 90 分钟，注意力撑不住
    version: str = RULES_VERSION

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "weekly_minutes": self.weekly_minutes,
            "new_ratio": self.new_ratio,
            "review_weeks": list(self.review_weeks),
            "min_task_minutes": self.min_task_minutes,
            "max_task_minutes": self.max_task_minutes,
        }


DEFAULT_PARAMS = PlanParams()


@dataclass
class NormalizedTask:
    week_index: int
    subject: str
    focus: str = ""
    task_type: str = TYPE_NEW
    minutes: int = 0
    related_item_ids: list[str] = field(default_factory=list)
    basis: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "week_index": self.week_index,
            "subject": self.subject,
            "focus": self.focus,
            "task_type": self.task_type,
            "minutes": self.minutes,
            "related_item_ids": list(self.related_item_ids),
            "basis": dict(self.basis),
        }


def _basis(rule: str, why: str, **params) -> dict:
    return {"rule": rule, "why": why, "params": params}


def _merge_basis(existing: dict | None, new: dict) -> dict:
    """把任务原有的依据（如「剩余进度」说明）并进新依据。

    不合并的话，`apply_remaining_ratio` 打的 `progress_note` 会在后面重建
    basis 时被覆盖掉——用户就永远看不到"计划为什么从中间开始"。
    """
    merged = dict(new)
    for key, value in (existing or {}).items():
        if key not in merged:
            merged[key] = value
    return merged


def _split_minutes(total: int, count: int, *, params: PlanParams) -> list[int]:
    """把一周的时长平均分给几个任务，并夹在 [min, max] 之间。

    夹到 min 之后总和可能超出容量——那种情况由调用方把多出来的任务顺延，
    而不是把每个任务都压到"读 3 分钟"这种做不成块的长度。
    """
    if count <= 0:
        return []
    each = max(params.min_task_minutes, total // count)
    each = min(each, params.max_task_minutes)
    return [each] * count


def apply_remaining_ratio(
    tasks: list[dict], *, remaining_ratio: float
) -> tuple[list[dict], dict | None]:
    """已读了一部分时，跳过前面那些已完成的任务。

    `remaining_ratio` 是"还剩多少没学"（1.0 = 从头开始）。
    按比例切掉任务列表的头部，并给第一个保留下来的任务打上说明——
    **不悄悄少排几条**：用户需要知道为什么计划从中间开始。
    """
    if remaining_ratio >= 0.99 or remaining_ratio <= 0:
        return tasks, None
    if not tasks:
        return tasks, None

    skip = int(len(tasks) * (1 - remaining_ratio))
    skip = max(0, min(skip, len(tasks) - 1))  # 至少留一个任务
    if skip == 0:
        return tasks, None

    kept = tasks[skip:]
    note = {
        "rule": "remaining_progress",
        "why": (
            f"你已经完成约 {round((1 - remaining_ratio) * 100)}%，"
            f"所以前 {skip} 个任务没有排进来——计划从剩下没学的部分开始。"
        ),
        "params": {"remaining_ratio": round(remaining_ratio, 3), "skipped_tasks": skip},
    }
    kept = list(kept)
    first = dict(kept[0])
    first_basis = dict(first.get("basis") or {})
    first_basis["progress_note"] = note
    first["basis"] = first_basis
    kept[0] = first          # 必须写回：只改副本的话这条说明永远不会生效
    return kept, note


def insert_reviews(
    tasks: list[NormalizedTask], *, params: PlanParams
) -> list[NormalizedTask]:
    """在指定周插入复习任务，内容是前面几周学过的主题。

    为什么由代码做：复习点必须**真的落在日历上**才有用。让模型自己"记得第 4 周复习第 1 周"，
    它在长列表里很容易忘；而这件事完全可以由规则保证——
    "第 N 周复习第 N-2 周的内容"是一条可执行的规则，不是判断。
    """
    if not tasks:
        return tasks
    by_week: dict[int, list[NormalizedTask]] = {}
    for task in tasks:
        by_week.setdefault(task.week_index, []).append(task)

    existing_weeks = sorted(by_week)
    added: list[NormalizedTask] = []
    for week in existing_weeks:
        if week not in params.review_weeks:
            continue
        # 复习两周前的主题；没有就往前找最近的一周
        source_week = next((w for w in reversed(existing_weeks) if w < week), None)
        if source_week is None:
            continue
        sources = by_week.get(source_week) or []
        if not sources:
            continue
        subjects = "、".join(t.subject for t in sources[:2])
        minutes = max(
            params.min_task_minutes,
            int(params.weekly_minutes * (1 - params.new_ratio) / max(1, len(sources))),
        )
        added.append(NormalizedTask(
            week_index=week,
            subject=f"回看：{subjects}",
            focus="不学新的，只把第 {} 周的内容重新过一遍".format(source_week),
            task_type=TYPE_REVIEW,
            minutes=minutes,
            related_item_ids=[i for t in sources for i in t.related_item_ids][:10],
            basis=_basis(
                "review_point",
                f"第 {week} 周回看第 {source_week} 周的内容——间隔重复通常落在第 2、4 周，"
                f"这时回看的留存率最高。",
                week=week, source_week=source_week,
            ),
        ))
    return list(tasks) + added


def normalize(
    tasks: list[dict],
    *,
    params: PlanParams | None = None,
    remaining_ratio: float = 1.0,
) -> tuple[list[NormalizedTask], dict[str, Any]]:
    """把模型给的任务列表变成可执行的排期。

    输入是 `GeneratedTask.model_dump()` 形状（week_index / subject / focus / related_item_ids），
    输出是带时长、类型、依据的任务 + 本次用的参数快照。
    """
    params = params or DEFAULT_PARAMS
    raw = [dict(t) for t in tasks if str(t.get("subject") or "").strip()]
    # 先按周稳定排序：模型给的任务顺序通常已经是教学顺序，不要打乱它
    raw.sort(key=lambda t: int(t.get("week_index") or 1))
    raw, progress_note = apply_remaining_ratio(raw, remaining_ratio=remaining_ratio)

    by_week: dict[int, list[dict]] = {}
    for task in raw:
        week = max(1, int(task.get("week_index") or 1))
        by_week.setdefault(week, []).append(task)

    notes: list[str] = []
    if progress_note:
        notes.append(progress_note["why"])

    normalized: list[NormalizedTask] = []
    deferred = 0
    for week in sorted(by_week):
        items = by_week[week]
        # 复习留出固定比例的时间；本周没东西可复习时（第 1 周）全部给新学
        has_review_week = week in params.review_weeks
        budget = int(params.weekly_minutes * (params.new_ratio if has_review_week else 1.0))
        per = _split_minutes(budget, len(items), params=params)

        # 装不下的顺延到下一周：不压碎任务，也不偷偷丢掉
        capacity = params.weekly_minutes
        used = 0
        overflow: list[dict] = []
        for index, (task, minutes) in enumerate(zip(items, per)):
            if used + minutes > capacity:
                overflow.append(task)
                continue
            used += minutes
            normalized.append(NormalizedTask(
                week_index=week,
                subject=str(task.get("subject") or "").strip()[:256],
                focus=str(task.get("focus") or "").strip()[:300],
                task_type=TYPE_NEW,
                minutes=minutes,
                related_item_ids=[str(i) for i in (task.get("related_item_ids") or [])][:10],
                basis=_merge_basis(task.get("basis"), _basis(
                    "weekly_capacity",
                    (
                        f"本周排了 {len(items) - len(overflow)} 个任务、共 {used} 分钟，"
                        f"没超过你每周 {params.weekly_minutes} 分钟的可用时间。"
                    ),
                    week=week, weekly_minutes=params.weekly_minutes, minutes=minutes,
                )),
            ))
        if overflow:
            deferred += len(overflow)
            next_week = week + 1
            for task in overflow:
                normalized.append(NormalizedTask(
                    week_index=next_week,
                    subject=str(task.get("subject") or "").strip()[:256],
                    focus=str(task.get("focus") or "").strip()[:300],
                    task_type=TYPE_NEW,
                    minutes=params.min_task_minutes,
                    related_item_ids=[str(i) for i in (task.get("related_item_ids") or [])][:10],
                    basis=_merge_basis(task.get("basis"), _basis(
                        "weekly_capacity",
                        (
                            f"这周已经排满 {params.weekly_minutes} 分钟，"
                            f"所以顺延到第 {next_week} 周——比强行塞进来更现实。"
                        ),
                        week=next_week, deferred_from=week, weekly_minutes=params.weekly_minutes,
                    )),
                ))
            notes.append(
                f"第 {week} 周任务超出周容量，{len(overflow)} 个任务顺延到第 {next_week} 周。"
            )

    normalized = insert_reviews(normalized, params=params)
    normalized.sort(key=lambda t: (t.week_index, 0 if t.task_type == TYPE_NEW else 1))

    snapshot = {
        **params.as_snapshot(),
        "remaining_ratio": round(remaining_ratio, 3),
        "task_count": len(normalized),
        "review_count": sum(1 for t in normalized if t.task_type == TYPE_REVIEW),
        "deferred": deferred,
        "notes": notes,
    }
    return normalized, snapshot


def describe_snapshot(snapshot: dict) -> str:
    """把快照翻成一句人话，给计划页头部用。"""
    if not snapshot:
        return ""
    total = int(snapshot.get("task_count") or 0)
    parts = [
        f"共 {total} 个任务" if total else "还没有任务",
        f"每周可用 {snapshot.get('weekly_minutes', 0)} 分钟",
        f"新学占 {round(float(snapshot.get('new_ratio') or 0) * 100)}%",
    ]
    review_weeks = snapshot.get("review_weeks") or []
    if review_weeks:
        parts.append("第 " + "、".join(str(w) for w in review_weeks) + " 周安排回看")
    if float(snapshot.get("remaining_ratio") or 1) < 0.99:
        parts.append(f"只排剩下没学的 {round(float(snapshot['remaining_ratio']) * 100)}%")
    return "；".join(p for p in parts if p) + "。"


def task_type_label(task_type: str) -> str:
    return _TYPE_LABELS.get(task_type, "")


def weeks_of(tasks: list) -> int:
    """计划跨度（周数）。空计划返回 0。"""
    weeks = {int(getattr(t, "week_index", 0) or 0) for t in tasks}
    weeks.discard(0)
    return len(weeks)


def round_minutes(value: float) -> int:
    """时长统一取整到 5 分钟：精确到分钟是假精度，没人按 37 分钟执行。"""
    return int(math.ceil(value / 5.0) * 5)
