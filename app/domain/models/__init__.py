"""领域模型（DOMAIN）—— 用户/知识/主张/行为事件/会话/目标/计划/任务/成本。

遵循架构铁律：跨能力写操作一律经此层仓储，仓储强制 user_id 过滤（结构性防越权）。
"""
from app.domain.models.base import Base
from app.domain.models.user import User
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.models.book import Book
from app.domain.models.claim import Claim
from app.domain.models.conflict import Conflict  # 显式 import：未导入时 SQLAlchemy metadata 不会注册此表，init_db / alembic 都会跳过建表
from app.domain.models.learning_event import LearningEvent
from app.domain.models.conversation import Conversation
from app.domain.models.learning_goal import LearningGoal
from app.domain.models.learning_plan import LearningPlan
from app.domain.models.plan_task import PlanTask
from app.domain.models.task_run import TaskRun
from app.domain.models.cost_log import CostLog
from app.domain.models.push_job import PushJob
from app.domain.models.push_log import PushLog
from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis

__all__ = [
    "Base",
    "User",
    "KnowledgeItem",
    "Book",
    "Claim",
    "Conflict",
    "LearningEvent",
    "Conversation",
    "LearningGoal",
    "LearningPlan",
    "PlanTask",
    "TaskRun",
    "CostLog",
    "PushJob",
    "PushLog",
    "CognitiveDiagnosis",
]