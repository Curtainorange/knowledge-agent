"""主动开场：会话开启时，由副驾先说话。

与 `orchestrator.chat` 的分工很清楚：那个是「用户说了话 → 模型回应」，
这里是「用户还没说话 → 副驾先说」。认知副驾要是学习搭档，就不能摆一个空白输入框
等人下指令——**先开口、把话题接起来**，才是它该有的样子。

**为什么不调模型**，两条理由：

1. 开场是每次新会话都要跑的一步，调模型等于给「打开对话框」这个动作绑上成本和延迟；
2. 它要说的事（存了多少条、有没有目标、有没有没处理的冲突）本来就是可枚举的状态。
   拼出来的话比模型自由发挥更准、更可控，也才能被测试钉住——
   换成模型生成，「开场里必须包含待处理冲突数」这种断言就写不出来了。

文案只守一条规矩：**每条开场都以一个具体问题收尾**。
主动提问才有引导力；「你可以做 A、B、C」只是把功能清单换个说法，
用户仍要自己做选择——那恰恰是这次要改掉的东西。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
)

logger = logging.getLogger(__name__)

# 目标描述写进开场时截断：一句话里塞进一个 200 字的书面目标，开场就变成了朗读
_GOAL_SNIPPET = 24


@dataclass(frozen=True)
class UserState:
    """开场说什么，取决于这几个事实。"""

    knowledge_count: int = 0
    goal: str = ""
    has_plan: bool = False
    unseen_conflicts: int = 0


def collect_state(session: Session, *, user_id: str) -> UserState:
    """读用户当前状态。

    每一项读失败都降级成「没有」，绝不让开场跟着失败：开场是进对话的第一步，
    它挂掉用户就对着一个空屏，比少说一句严重得多。
    """
    knowledge_count = 0
    goal = ""
    has_plan = False
    unseen = 0

    try:
        knowledge_count = KnowledgeRepository(session, user_id=user_id).count_active(user_id)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("opening: 读知识条目数失败，按 0 处理: %s", exc)

    try:
        goals = LearningGoalRepository(session, user_id=user_id).list_active(user_id)
        if goals and (goals[0].description or "").strip():
            goal = goals[0].description.strip()
            # 计划是「挂在目标上的」，没有目标就谈不上有没有计划
            plan = LearningPlanRepository(session, user_id=user_id).latest_for_goal(goals[0].id)
            has_plan = plan is not None
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("opening: 读目标 / 计划失败，按「无」处理: %s", exc)

    try:
        counted = ConflictRepository(session, user_id=user_id).count_by_state(user_id)
        unseen = int((counted or {}).get("unseen", 0) or 0)
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("opening: 读冲突数失败，按 0 处理: %s", exc)

    return UserState(
        knowledge_count=knowledge_count,
        goal=goal,
        has_plan=has_plan,
        unseen_conflicts=unseen,
    )


def build_opening(state: UserState) -> str:
    """按状态拼一句开场。

    分支顺序 = **话题的时效性**，不是能力清单的顺序：
    「有东西等着你处理」永远排在「你可以做点什么」前面。
    """
    goal = _short(state.goal)

    # 1. 有没处理的冲突 —— 唯一一种「事情已经发生了、在等你」的状态
    if state.unseen_conflicts:
        return (
            f"我是你的认知副驾。上次扫出来的 {state.unseen_conflicts} 处冲突还没处理——"
            "要现在过一遍吗？还是先做别的？"
        )

    # 2. 有目标但没计划 —— 中间状态，最该主动补上的断点
    if goal and not state.has_plan:
        return (
            f"我是你的认知副驾。你的目标「{goal}」还没有周计划——"
            "要我现在拆成一周的任务吗？"
        )

    # 3. 目标与计划都在 —— 问「接着走」还是「回头看」
    if goal:
        return (
            f"我是你的认知副驾。目标「{goal}」在推进中。"
            "今天想接着往下走，还是先看看执行有没有偏离？"
        )

    # 4. 有积累但没目标 —— 库里有内容可聊，先给一个能立刻做的
    if state.knowledge_count:
        return (
            f"我是你的认知副驾。你知识库里已经存了 {state.knowledge_count} 条——"
            "想让我扫一遍看有没有互相矛盾的说法吗？"
        )

    # 5. 全空 —— 第一句话，把「怎么用」和「先做哪件」一起说掉
    return (
        "我是你的认知副驾——你学习时的搭档，不用记功能按钮，说人话就行。"
        "这里现在还是空的：你可以直接把想记的东西说给我听，"
        "也可以告诉我你最近在学什么，我们先立一个目标。想从哪儿开始？"
    )


def opening_for(session: Session, *, user_id: str) -> str:
    """读状态 + 拼开场（调用方只需这一个函数）。"""
    return build_opening(collect_state(session, user_id=user_id))


def _short(text: str, limit: int = _GOAL_SNIPPET) -> str:
    """把目标描述压成能塞进一句话的长度。"""
    cleaned = (text or "").strip().replace("\n", " ")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"
