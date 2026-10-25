"""用户偏好端点（系统设计 §7.1 / §8.1）：推送频率 + 排期参数。

两部分能力：

1. **推送频率**（UC-G-02）三档：weekly（默认）/ daily / quiet。
   它是 ADR-14 推送与抑制服务的输入之一——quiet 用户在抑制决策里会被
   「免打扰」规则拦下，不产生常规推送。
2. **排期参数**（F 模块）：每周可用分钟 / 新学占比 / 复习周 / 单任务时长区间。
   10-20 的方案要求计划页「显示参数并可改」，此前只做了显示，参数硬编码在
   `plan_rules.DEFAULT_PARAMS`——一个每周只有两小时的人，拿着「每周 5 小时」
   的容量，计划从第一天就排不完。

两条约定：

- **读出来的永远是「生效中的值」**：用户没设过就返回默认值，而不是返回 null
  让前端自己补默认——两处各补一套默认，迟早分叉。
- **写入必过 `plan_rules.from_prefs()`**：夹紧越界、丢弃类型错误、补全缺项。
  偏好是用户能改的，就必须假定它是脏的。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent import plan_rules
from app.api.deps import get_current_user, get_session
from app.core import trace
from app.domain.models.user import User
from app.domain.repositories.user_repository import UserRepository

router = APIRouter(prefix="/api/v1/preferences", tags=["preferences"])

VALID_FREQUENCIES = ("weekly", "daily", "quiet")


class PlanParamsIn(BaseModel):
    """排期参数的部分更新。全部可选：只改传上来的项。

    传一个**空对象** `{}` 表示「恢复默认」——比另开一个 reset 布尔字段清晰，
    也让前端一个「恢复默认」按钮就能收口。
    """

    weekly_minutes: int | None = None
    new_ratio: float | None = None
    review_weeks: list[int] | None = None
    min_task_minutes: int | None = None
    max_task_minutes: int | None = None


class PreferencesOut(BaseModel):
    push_frequency: str
    plan_params: dict = Field(default_factory=dict)
    request_id: str


class PreferencesUpdate(BaseModel):
    # 两个都可选：前端改其中一项时不必把另一项原样回传（回传就有覆盖风险）
    push_frequency: str | None = Field(default=None, pattern="^(weekly|daily|quiet)$")
    plan_params: PlanParamsIn | None = None


def _effective_params(user: User) -> dict:
    """当前生效的排期参数（没设过就是默认值）。"""
    return plan_rules.to_prefs(plan_rules.from_prefs(getattr(user, "plan_params", None)))


@router.get("", response_model=PreferencesOut)
def get_preferences(
    user: User = Depends(get_current_user),
) -> PreferencesOut:
    return PreferencesOut(
        push_frequency=user.push_frequency or "weekly",
        plan_params=_effective_params(user),
        request_id=trace.get_request_id() or "",
    )


@router.put("", response_model=PreferencesOut)
def update_preferences(
    body: PreferencesUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> PreferencesOut:
    repo = UserRepository(session)
    if body.push_frequency:
        repo.set_push_frequency(user, body.push_frequency)

    if body.plan_params is not None:
        patch = body.plan_params.model_dump(exclude_none=True)
        if not patch:
            repo.set_plan_params(user, None)  # 空对象 = 恢复默认
        else:
            # 先取出当前生效值再合并：前端只想改「每周分钟」时，
            # 不该把其他三项悄悄重置成默认
            merged = _effective_params(user)
            merged.update(patch)
            repo.set_plan_params(user, plan_rules.to_prefs(plan_rules.from_prefs(merged)))

    session.commit()
    return PreferencesOut(
        push_frequency=user.push_frequency or "weekly",
        plan_params=_effective_params(user),
        request_id=trace.get_request_id() or "",
    )
