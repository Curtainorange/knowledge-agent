"""理解度探查记录（E 模块）：把「没摘录」和「没读懂」分开。

## 为什么需要这张表

L5 原来的信号全是行为计数（完成比 / 活跃度 / 闲置天数 / 未解冲突数），
于是「你很少做摘录」被直接归因成「你没读懂」——这条推断对**已经懂了、只是懒得记**
的读者是彻底的误伤。

要区分开，只有一条路：**问一次**。而且不能问「你懂了吗」（谁都说懂），
要问一道有对错的判断题，再按回答决定要不要追问复述。

## 为什么记录两轮而不是一轮

- 第 1 轮定"有没有基础"（接触史 + 判断题）。自评"第一次接触"的人，
  判错是正常的，不该被诊断成认知缺陷——那是还没学，不是没学懂。
- 第 2 轮定"是不是真懂"（复述）。只有当用户自称读过/能讲、判断题却答错时，
  才追问复述；否则是审问，不是诊断。

两轮都落在这里，判定矩阵（`probe.decide`）读这两轮的证据。
"""
from __future__ import annotations

from uuid import uuid4

from sqlalchemy import JSON, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.models.base import Base, TimestampMixin


class ComprehensionProbe(Base, TimestampMixin):
    __tablename__ = "comprehension_probes"
    __table_args__ = (Index("ix_probes_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), index=True)

    # 探查对象：一个主题标签 + 可选的具体条目。条目被删也不影响历史判定。
    topic: Mapped[str] = mapped_column(String(128), default="")
    item_id: Mapped[str | None] = mapped_column(String(36), nullable=True, default=None)

    round: Mapped[int] = mapped_column(Integer, default=1)  # 1 = 判断题 / 2 = 复述
    # 自评：first / once / teach（与 intake 的 self_level 同一套枚举）
    self_level: Mapped[str] = mapped_column(String(16), default="")

    # 第 1 轮：判断题。题干与选项必须落库——用户答到一半关掉页面再回来，
    # 要能把同一道题原样再摆出来，而不是换一道新题重问（那等于把上面的作答作废）。
    question: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[list | None] = mapped_column(JSON, default=list)
    judge_key: Mapped[str] = mapped_column(String(8), default="")  # 用户选的 a/b/c
    judge_correct: Mapped[bool | None] = mapped_column(nullable=True, default=None)

    # 第 2 轮：复述 + 代码侧覆盖率
    recall_text: Mapped[str] = mapped_column(Text, default="")
    coverage: Mapped[float] = mapped_column(Float, default=0.0)

    # 出题时定下的核心要点（第 2 轮判分要用，必须随题一起落库）
    key_points: Mapped[list | None] = mapped_column(JSON, default=list)
    covered: Mapped[list | None] = mapped_column(JSON, default=list)
    missed: Mapped[list | None] = mapped_column(JSON, default=list)

    # 判定结论：gap / mastered / bias / guide / unknown（见 probe.decide）
    verdict: Mapped[str] = mapped_column(String(16), default="")
