"""推送任务实体（系统设计 §6.5.3）：ADR-14 推送与抑制服务的持久化任务。

状态机：pending → sent / suppressed / failed；sent → delivered / failed。
- suppressed 是终态（被去重 / 收敛 / 免打扰拦下，不发送）
- failed 可回到 pending 重试，或终态入死信
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin

# 状态集合（供仓储校验，避免散落魔法字符串）
PUSH_JOB_STATES = ("pending", "suppressed", "sent", "delivered", "failed")


class PushJob(Base, TimestampMixin):
    __tablename__ = "push_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)
    push_type: Mapped[str] = mapped_column(String(32))  # conflict/question/brief/diagnosis
    channel: Mapped[str] = mapped_column(String(16), default="app")  # app/email（P0 仅 app）
    status: Mapped[str] = mapped_column(String(16), default="pending")
    title: Mapped[str] = mapped_column(String(255), default="")
    body: Mapped[str] = mapped_column(String(2000), default="")
    # 结构化内容（如冲突 id 列表 / 追问文本 / 诊断 id），供前端点击跳转与审计
    payload: Mapped[dict | None] = mapped_column(JSON, default=dict)
    # 去重指纹：同用户同类型同内容 hash 只保留一次（防骚扰）
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    sent_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str] = mapped_column(String(1000), default="")
