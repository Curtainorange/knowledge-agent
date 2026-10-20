"""主动开场：会话开启时，由副驾先说话。

与 `orchestrator.chat` 的分工很清楚：那个是「用户说了话 → 模型回应」，
这里是「用户还没说话 → 副驾先说」。认知副驾要是学习搭档，就不能摆一个空白输入框
等人下指令——**先开口、把话题接起来**，才是它该有的样子。

## 本轮改造（B 模块）：从「推动作」改成「问方向」

原实现按状态分五支，每支都直接给下一步动作：有冲突就问"要不要现在过一遍"、
有目标没计划就问"要我现在拆成周任务吗"、有积累就问"想让我扫一遍吗"。
问题不在文案，在**顺序**——用户还没说清这次要什么，系统已经替他决定了做什么。

现在开场只做两件事：

1. 把系统已经知道的摆出来（你最近在读《X》，读到第 3 章——这是推断，可以改）；
2. 问一句方向（今天接着读，还是先解决一个具体问题）。

未处理的冲突保留，但**降级成一句待办提示**：它是"事情在等你"，不是"我建议你做"。
区别在于用户可以说"先不管"而不觉得被推销。

## 为什么开场仍然不调模型

两条理由没变：开场是每次新会话的第一步，调模型等于给"打开对话框"绑成本与延迟；
它要说的事（读哪本、读到哪、有几处冲突）本来就可枚举，拼出来比模型自由发挥更准，
也才能被测试钉住。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.agent import intake
from app.agent.cards import reading_intake_card
from app.agent.conflict_view import ConflictBrief, top_unresolved
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.domain.repositories.learning_plan_repository import (
    LearningGoalRepository,
    LearningPlanRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserState:
    """开场说什么，取决于这几个事实。

    `top_conflict` 保留下来供**别处**使用（冲突卡片、诊断归因都要"最该处理的那一处"），
    但开场不再摆出双方主张——那是"给出议题"，属于建议行为，要等用户确认方向之后。
    """

    knowledge_count: int = 0
    goal: str = ""
    has_plan: bool = False
    unseen_conflicts: int = 0
    top_conflict: ConflictBrief | None = None


def collect_state(session: Session, *, user_id: str) -> UserState:
    """读用户当前状态。

    每一项读失败都降级成「没有」，绝不让开场跟着失败：开场是进对话的第一步，
    它挂掉用户就对着一个空屏，比少说一句严重得多。
    """
    knowledge_count = 0
    goal = ""
    has_plan = False
    unseen = 0
    top_conflict: ConflictBrief | None = None

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
        if unseen:
            briefs = top_unresolved(session, user_id=user_id, limit=1)
            top_conflict = briefs[0] if briefs else None
    except Exception as exc:  # pragma: no cover - 防御性兜底
        logger.warning("opening: 读冲突数失败，按 0 处理: %s", exc)

    return UserState(
        knowledge_count=knowledge_count,
        goal=goal,
        has_plan=has_plan,
        unseen_conflicts=unseen,
        top_conflict=top_conflict,
    )


def opening_text(state: UserState, ctx: intake.IntakeContext | None = None) -> str:
    """开场文本。**只有两个分支**：

    - 库空：说明现状 + 给两个入口（记一条 / 同步），仍以问句收尾；
    - 有内容：交给 `intake.opening_text` 做上下文确认。

    分支从五个降到两个，是因为开场不再承担"推荐下一步"的职责——
    推荐要基于确认过的诉求，那是建议卡的事。
    """
    if state.knowledge_count == 0:
        return (
            "我是你的认知副驾。库里还是空的：你可以先说一句想记的东西，"
            "或者连上微信读书把划线和想法同步进来。想从哪儿开始？"
        )
    return intake.opening_text(
        ctx or intake.IntakeContext(),
        unseen_conflicts=state.unseen_conflicts,
    )


def opening_for(
    session: Session, *, user_id: str, conversation=None
) -> tuple[str, dict | None]:
    """读状态 + 生成开场（调用方只需这一个函数）。

    返回 `(文本, 卡片或 None)`：有内容可聊时带一张需求确认卡；库空时不发卡
    （没有内容可确认，问五件事也没用）。

    顺带把推断出的阅读上下文写回会话——用户刷新页面回来，不必重新说一遍在读什么。
    """
    state = collect_state(session, user_id=user_id)

    ctx = intake.IntakeContext()
    inferred: dict = {"book": None, "basis": "none"}
    if state.knowledge_count:
        try:
            inferred = intake.infer_reading_target(session, user_id=user_id)
        except Exception as exc:  # pragma: no cover - 推断失败不发卡也不阻断开场
            logger.warning("opening: 推断阅读目标失败: %s", exc)
        if inferred.get("book"):
            ctx.book = inferred["book"]
        ctx.refresh_missing()

    text = opening_text(state, ctx)

    card: dict | None = None
    if state.knowledge_count and ctx.missing:
        card = reading_intake_card(
            key="intake",
            round=ctx.round,
            inferred={"book": ctx.book, "basis": inferred.get("basis")},
            questions=intake.next_questions(ctx),
            digest_payload=intake.digest(ctx),
        )
        # 标记「确认卡已发出」：之后用户说的话会被当成回答，而不是闲聊
        intake.mark_card_sent(ctx)

    if conversation is not None:
        try:
            intake.save(session, conversation=conversation, ctx=ctx)
        except Exception as exc:  # pragma: no cover - 写上下文失败不该让开场挂掉
            logger.warning("opening: 写阅读上下文失败: %s", exc)

    return text, card
