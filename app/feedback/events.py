"""行为事件埋点（ADR-10 / 架构 §10.2）：append-only 的事件发射器。

两条铁律：

1. **绝不记录正文**。事件 payload 只放元数据（id、类型、长度、状态、计数），
   正文与凭证一律不进事件表——它与业务表在同一个库里，泄漏面一样大。
2. **埋点是旁路，不能反噬主链路**。发射器自己提交、自己吞异常：
   埋点失败只记一行 warning，绝不让「录入知识」这种主流程失败，也不参与主链路
   事务（避免长事务与回滚把事件带走）。

事件命名沿用架构设计 §12 的 `域.对象.动作` 风格，便于后续按前缀聚合。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.domain.repositories.learning_event_repository import LearningEventRepository

logger = logging.getLogger(__name__)

# 知识库
KNOWLEDGE_CREATED = "knowledge.item.created"
KNOWLEDGE_UPDATED = "knowledge.item.updated"
KNOWLEDGE_DELETED = "knowledge.item.deleted"
# 书籍与阅读
BOOK_UPLOADED = "book.uploaded"
BOOK_PROGRESS = "book.progress"
NOTE_CREATED = "note.created"
# 能力调用与反馈
L1_MINE = "l1.mine"
L2_SCAN = "l2.scan"
L2_CONFLICT_FEEDBACK = "l2.conflict.feedback"
L3_BRIEF = "l3.brief"
L3_QUESTION = "l3.question"
# 学习计划与路径修正
L4_GOAL_CREATED = "l4.goal.created"
L4_PLAN_GENERATED = "l4.plan.generated"
L4_DEVIATION_CHECKED = "l4.deviation.checked"
L4_ADJUSTMENT_DECIDED = "l4.adjustment.decided"
# L5 归因诊断
L5_DIAGNOSIS_CREATED = "l5.diagnosis.created"
L5_DIAGNOSIS_DECIDED = "l5.diagnosis.decided"
# 推送（ADR-14）
PUSH_SUPPRESSED = "push.suppressed"
PUSH_SENT = "push.sent"
PUSH_FEEDBACK = "push.feedback"
# 认证（L5 需要区分「活跃但无产出」与「根本不活跃」）
AUTH_LOGIN = "auth.login"


def record(session: Session, *, user_id: str, event_type: str, payload: dict | None = None) -> None:
    """发射一条行为事件。任何异常都被吞掉并记日志——埋点失败不得影响业务。

    失败时用 **SAVEPOINT**（begin_nested）回滚，而不是整事务 rollback：
    后者会在埋点出错时把调用方尚未提交的业务改动一起丢掉，那才是真的反噬。
    """
    try:
        with session.begin_nested():  # 埋点自带保存点，失败只回滚这一小段
            LearningEventRepository(session).append(
                user_id=user_id, event_type=event_type, payload=payload or {}
            )
        session.commit()  # 旁路独立提交：不随主链路事务回滚
    except Exception as exc:  # noqa: BLE001 - 埋点是尽力而为
        logger.warning("learning event dropped type=%s user=%s err=%s", event_type, user_id, exc)