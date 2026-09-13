"""推送与抑制服务（ADR-14）：统一决定「什么该推、多久推一次、什么不该推」。

设计要点（系统设计 §7）：

1. **抑制先于发送**：任何推送在生成任务前先过 `suppress()` 三道闸门——
   - 免打扰：`push_frequency == quiet` 且非「重大诊断」→ 抑制
   - 去重：同一 `content_hash` 已推送过（PushLog 唯一）→ 抑制
   - 收敛：同类冲突被用户忽略 ≥N 次 → 抑制该类推荐（UC-L2-03 扩展 2a）
2. **内容与决策分离**：`build_weekly_digest` 只收集本地可算的计数（冲突数 / 新增知识 /
   活跃度），模型不参与算术；抑制规则也全在代码里，不调模型。
3. **推送是旁路**：enqueue 失败只记日志与 suppressed 事件，绝不影响调用方主链路。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.learning_event_repository import LearningEventRepository
from app.domain.repositories.push_repository import PushJobRepository, PushLogRepository
from app.domain.repositories.user_repository import UserRepository
from app.feedback import events

logger = logging.getLogger(__name__)

SUPPRESS_QUIET = "quiet 免打扰"
SUPPRESS_DUPLICATE = "重复内容"
SUPPRESS_CONVERGED = "同类已被忽略多次"


def content_hash(user_id: str, push_type: str, subject: str) -> str:
    """内容去重指纹：同用户 + 同类型 + 同主题内容稳定映射到同一 hash。

    subject 是「内容主体」——冲突推送用 conflict_id、周简报用 ISO 周、
    诊断用 diagnosis_id，保证同一周 / 同一冲突 / 同一诊断只推一次。
    """
    raw = f"{user_id}|{push_type}|{subject}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class Digest:
    """周简报数据（全部本地可算，模型不参与算术）。"""

    week_label: str
    conflict_total: int = 0
    conflict_unseen: int = 0
    new_items: int = 0
    study_events: int = 0
    title: str = ""
    body: str = ""


@dataclass
class EnqueueOutcome:
    status: str  # pending | suppressed
    reason: str = ""
    job_id: str = ""


class PushService:
    def __init__(self, session: Session):
        self._session = session

    # ---- 抑制决策（纯本地，可单测）-----------------------------------------

    def suppress(
        self, *, user_id: str, push_type: str, content_hash: str, conflict_type: str = ""
    ) -> tuple[bool, str]:
        """返回 (是否抑制, 原因)。三道闸门按优先级短路。"""
        user = UserRepository(self._session).get(user_id)
        if user is not None and (user.push_frequency or "weekly") == "quiet":
            if push_type != "diagnosis":
                return True, SUPPRESS_QUIET

        if PushLogRepository(self._session, user_id=user_id).exists(user_id, content_hash):
            return True, SUPPRESS_DUPLICATE

        if push_type == "conflict" and conflict_type:
            ignored = ConflictRepository(self._session, user_id=user_id).ignored_type_counts(user_id)
            if ignored.get(conflict_type, 0) >= settings.l2_ignore_suppress_threshold:
                return True, SUPPRESS_CONVERGED

        return False, ""

    # ---- 入队（经抑制决策）-------------------------------------------------

    def enqueue(
        self,
        *,
        user_id: str,
        push_type: str,
        title: str,
        body: str,
        payload: dict | None = None,
        subject: str = "",
        conflict_type: str = "",
        channel: str = "app",
    ) -> EnqueueOutcome:
        """生成推送任务。subject 为空时用 title 做去重主体。"""
        digest = content_hash(user_id, push_type, subject or title)
        suppressed, reason = self.suppress(
            user_id=user_id, push_type=push_type, content_hash=digest, conflict_type=conflict_type
        )
        if suppressed:
            events.record(
                self._session, user_id=user_id, event_type=events.PUSH_SUPPRESSED,
                payload={"push_type": push_type, "reason": reason},
            )
            return EnqueueOutcome(status="suppressed", reason=reason)

        job = PushJobRepository(self._session, user_id=user_id).create(
            user_id=user_id,
            push_type=push_type,
            content_hash=digest,
            title=title,
            body=body,
            payload=payload or {},
            channel=channel,
        )
        PushLogRepository(self._session, user_id=user_id).create(
            user_id=user_id, push_type=push_type, content_hash=digest, channel=channel
        )
        self._session.commit()
        events.record(
            self._session, user_id=user_id, event_type=events.PUSH_SENT,
            payload={"push_type": push_type, "job_id": job.id},
        )
        return EnqueueOutcome(status="pending", job_id=job.id)

    # ---- 周简报组装（本地可算）---------------------------------------------

    def build_weekly_digest(self, *, user_id: str, now: datetime | None = None) -> Digest:
        """收集本周冲突数 / 新增知识 / 活跃度，组装周简报（模型不参与算术）。"""
        now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
        week_start = now - timedelta(days=7)
        week_label = f"{now.year}-W{now.isocalendar()[1]}"

        conflicts = ConflictRepository(self._session, user_id=user_id).list_by_user(user_id, limit=500)
        recent = [c for c in conflicts if c.created_at and c.created_at >= week_start]
        unseen = sum(1 for c in recent if c.user_state == "unseen")

        event_repo = LearningEventRepository(self._session, user_id=user_id)
        study = [e for e in event_repo.list_recent(user_id, limit=1000)
                 if e.event_type in (events.KNOWLEDGE_CREATED, events.NOTE_CREATED,
                                     events.BOOK_PROGRESS, events.L1_MINE)]
        recent_study = [e for e in study if e.occurred_at and e.occurred_at >= week_start]
        new_items = sum(
            1 for e in recent_study if e.event_type == events.KNOWLEDGE_CREATED
        )

        title = f"认知简报 - {week_label}"
        body = (
            f"本周新增 {new_items} 条知识，学习行为 {len(recent_study)} 次；"
            f"检测到 {len(recent)} 处观点冲突（其中 {unseen} 处待处理）。"
        )
        return Digest(
            week_label=week_label,
            conflict_total=len(recent),
            conflict_unseen=unseen,
            new_items=new_items,
            study_events=len(recent_study),
            title=title,
            body=body,
        )

    # ---- 状态推进（P0：app 内通知，pending 即待查看，简化状态机）-------------

    def mark_delivered(self, *, user_id: str, job_id: str) -> None:
        job = PushJobRepository(self._session, user_id=user_id).get(job_id)
        if job is None:
            return
        PushJobRepository(self._session, user_id=user_id).set_status(job, "delivered")
        self._session.commit()
