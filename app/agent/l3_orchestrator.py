"""L3 认知助产编排（系统设计 §5.3 / 需求 UC-L3-01、UC-L3-02）。

目标：生成用户「该问但没问」的深度问题。与 L1/L2 的关键差别是**它不检索用户已知，
而是指出认知结构里的缺口**——所以它的产出必须带数据依据，否则就是心灵鸡汤。

两个入口：
- `brief()`           UC-L3-01 的主体（不含推送）：主题分布 + 认知深度 → 识别
                      「大量存在 / 完全缺失」模式 → 生成 2-3 个启发式追问 + 冲突同比
- `question_for_item()` UC-L3-02：围绕刚录入的内容，生成 1 个与之衔接的深度追问

关键工程决策：
1. **计数在代码里做、不在模型里做**。第一步只让模型做「逐条归类」
   （item_id → topic/level），分布统计由本地聚合得出——模型算数不可靠，
   而「3 篇 / 12 篇」这种数字恰恰是追问的依据，错一个就不可信了。
2. **两步都走 reasoning=off**（topic_analysis / cognitive_brief）。分类与生成式
   提问不需要深度推理，省下的思考溢价直接体现为成本。
3. **不做自动触发**：UC-L3-02 说「收藏后」触发，但主动推送要等 ADR-14 推送与抑制
   服务（否则每次录入都多一次模型调用且无处送达）。当前只提供被动调用入口。

已知不足（实测观察）：主题粒度不稳定。条目少且领域分散时，模型倾向给每条一个独立
标签（实测 6 条「如何开始 X」被拆成 6 个主题，而非合并为「入门方法」）。这与 L2 的
topic 标签问题是同一个根因——**LLM 自由生成标签天然不稳定**。影响有限（追问仍基于
真实计数，只是分布表的可读性波动）；条目量上来后同领域重复出现，标签会自然收敛。
彻底解法是本地按向量聚类替代模型命名，留待 L2 的 pgvector 阶段一并做。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.conflict_view import unresolved_section
from app.core.config import settings
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.gateway import ModelGateway
from app.llm.prompts import L3_ANALYZE, L3_BRIEF, L3_ITEM
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

_LEVELS = ("入门", "进阶", "实战", "未分类")


class TopicAssignment(BaseModel):
    item_id: str
    topic: str = Field(default="", max_length=32)
    level: Literal["入门", "进阶", "实战", "未分类"] = "未分类"


class TopicAnalysisResult(BaseModel):
    assignments: list[TopicAssignment] = Field(default_factory=list)
    overview: str = ""


class DepthQuestion(BaseModel):
    question: str
    why: str = ""        # 为什么问这个（与知识结构的关联）
    evidence: str = ""   # 数据依据（必须引用统计数字）
    next_step: str = ""  # 可执行动作


class BriefDraft(BaseModel):
    patterns: list[str] = Field(default_factory=list)  # 大量存在 / 完全缺失
    questions: list[DepthQuestion] = Field(default_factory=list)
    overview: str = ""


# 提示词统一在 app/llm/prompts.py 声明（版本化 + golden set 校验），此处仅取别名
_ANALYZE_SYS = L3_ANALYZE.text
_BRIEF_SYS = L3_BRIEF.text
_ITEM_SYS = L3_ITEM.text


@dataclass
class TopicStat:
    topic: str
    count: int
    levels: dict[str, int] = field(default_factory=dict)


@dataclass
class L3Brief:
    state: Literal["ok", "empty", "degraded"]
    analyzed_items: int = 0
    topics: list[TopicStat] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    questions: list[DepthQuestion] = field(default_factory=list)
    conflict_stats: dict = field(default_factory=dict)
    overview: str = ""
    note: str = ""


class L3Orchestrator:
    def __init__(self, gateway: ModelGateway, session: Session):
        self._gateway = gateway
        self._session = session

    # ---- UC-L3-01 认知简报 -------------------------------------------------

    def brief(self, *, user_id: str) -> L3Brief:
        items = KnowledgeRepository(self._session, user_id=user_id).list_active(user_id)
        if not items:
            return L3Brief(state="empty", note="知识库还是空的，先录入几条再来做认知分析。")

        digest = items[: settings.l3_max_items_per_analysis]
        assignments = self._classify(digest, user_id=user_id)
        if assignments is None:
            return L3Brief(
                state="degraded",
                analyzed_items=len(digest),
                note="主题归类失败（模型输出无法解析），请稍后重试。",
            )

        topics = self._aggregate(assignments)
        conflicts = self._conflict_stats(user_id)

        if not topics:
            return L3Brief(
                state="degraded", analyzed_items=len(digest), conflict_stats=conflicts,
                note="没有可用的主题归类结果，无法生成追问。",
            )

        draft = self._draft_brief(topics, digest, conflicts, user_id=user_id)
        if draft is None:
            return L3Brief(
                state="degraded", analyzed_items=len(digest), topics=topics,
                conflict_stats=conflicts,
                note="追问生成失败（模型输出无法解析），主题分布仍然有效。",
            )

        return L3Brief(
            state="ok",
            analyzed_items=len(digest),
            topics=topics,
            patterns=draft.patterns,
            questions=draft.questions[: settings.l3_question_count],
            conflict_stats=conflicts,
            overview=draft.overview,
        )

    # ---- UC-L3-02 单条内容的衔接追问 ---------------------------------------

    def question_for_item(self, *, user_id: str, item_id: str) -> L3Brief:
        repo = KnowledgeRepository(self._session, user_id=user_id)
        item = repo.get(item_id)
        if item is None or item.is_deleted:
            return L3Brief(state="empty", note="条目不存在。")

        recent = [i.title for i in repo.list_active(user_id)[: settings.l3_recent_titles]]
        context = (
            f"新录入的条目：\n标题：{item.title}\n"
            f"正文长度：{len(item.raw_content or '')} 字\n"
            f"正文摘要：{(item.raw_content or '')[:200]}\n\n"
            f"近期条目标题（共 {len(recent)} 条）：\n" + "\n".join(f"- {t}" for t in recent)
        )
        try:
            completion = self._gateway.chat(
                task_type="cognitive_brief",
                messages=[{"role": "system", "content": _ITEM_SYS},
                          {"role": "user", "content": context}],
                user_id=user_id,
                session=self._session,
                prompt_version=L3_ITEM.version,
                json_model=BriefDraft,
            )
            draft = parse_structured(completion.text, validator=lambda d: BriefDraft(**d))
        except JsonParseError as exc:
            logger.warning("l3 item question failed item=%s: %s", item_id, exc)
            return L3Brief(state="degraded", analyzed_items=1, note="追问生成失败，请稍后重试。")

        return L3Brief(
            state="ok",
            analyzed_items=1,
            questions=draft.questions[:1],
            patterns=draft.patterns,
            overview=draft.overview,
        )

    # ---- 内部：归类、聚合、生成 --------------------------------------------

    def _classify(self, items: list[KnowledgeItem], *, user_id: str) -> list[TopicAssignment] | None:
        lines = [
            f"[{i + 1}] item_id={item.id} 标题={item.title} 摘要={item.snippet}"
            for i, item in enumerate(items)
        ]
        messages = [
            {"role": "system", "content": _ANALYZE_SYS},
            {"role": "user", "content": "知识条目：\n" + "\n".join(lines)},
        ]
        try:
            completion = self._gateway.chat(
                task_type="topic_analysis", messages=messages, user_id=user_id, session=self._session,
                prompt_version=L3_ANALYZE.version, json_model=TopicAnalysisResult,
            )
            result = parse_structured(completion.text, validator=lambda d: TopicAnalysisResult(**d))
        except JsonParseError as exc:
            logger.warning("l3 classify failed: %s", exc)
            return None

        valid_ids = {item.id for item in items}
        # 丢弃模型幻觉出来的 id，避免把不存在的条目算进统计
        return [a for a in result.assignments if a.item_id in valid_ids]

    @staticmethod
    def _aggregate(assignments: list[TopicAssignment]) -> list[TopicStat]:
        """本地聚合主题分布（计数不交给模型算——数字是追问的依据，必须准确）。"""
        buckets: dict[str, TopicStat] = {}
        for a in assignments:
            topic = (a.topic or "").strip() or "未分类"
            stat = buckets.setdefault(topic, TopicStat(topic=topic, count=0))
            stat.count += 1
            stat.levels[a.level] = stat.levels.get(a.level, 0) + 1
        # 条目多的主题在前；同数量按名称稳定排序，避免每次刷新顺序抖动
        return sorted(buckets.values(), key=lambda s: (-s.count, s.topic))

    def _conflict_stats(self, user_id: str) -> dict:
        """本周与上周的冲突数（UC-L3-01 第 4 步「汇总本周冲突同比」）。"""
        xrepo = ConflictRepository(self._session, user_id=user_id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        week_ago = now - timedelta(days=7)
        two_weeks_ago = now - timedelta(days=14)
        rows = xrepo.list_by_user(user_id, limit=500)
        this_week = sum(1 for r in rows if r.created_at and r.created_at >= week_ago)
        last_week = sum(
            1 for r in rows
            if r.created_at and two_weeks_ago <= r.created_at < week_ago
        )
        by_state = xrepo.count_by_state(user_id)
        return {
            "this_week": this_week,
            "last_week": last_week,
            "delta": this_week - last_week,
            "by_state": by_state,
        }

    def _draft_brief(
        self,
        topics: list[TopicStat],
        items: list[KnowledgeItem],
        conflicts: dict,
        *,
        user_id: str,
    ) -> BriefDraft | None:
        lines = []
        for stat in topics:
            levels = "、".join(f"{k} {v} 篇" for k, v in sorted(stat.levels.items(), key=lambda kv: -kv[1]))
            lines.append(f"- {stat.topic}：共 {stat.count} 篇（{levels}）")
        titles = "\n".join(f"- {i.title}" for i in items[:30])
        context = (
            f"知识库共 {len(items)} 条，主题分布：\n" + "\n".join(lines)
            + f"\n\n本周新增冲突 {conflicts.get('this_week', 0)} 处，"
              f"上周 {conflicts.get('last_week', 0)} 处。"
        )
        # 冲突**内容**（而不只是数量）才是追问的原料：用户知识里互相打架的地方，
        # 本身就是「该问但没问」的最强候选——只给一个计数，模型无从针对它提问。
        unresolved = unresolved_section(self._session, user_id=user_id)
        if unresolved:
            context += (
                "\n\n尚未处理的观点冲突（生成追问时优先围绕它们——这些是用户自己"
                "知识里互相矛盾的地方）：\n" + unresolved
            )
        context += f"\n\n部分条目标题：\n{titles}"
        try:
            completion = self._gateway.chat(
                task_type="cognitive_brief",
                messages=[{"role": "system", "content": _BRIEF_SYS},
                          {"role": "user", "content": context}],
                user_id=user_id,
                session=self._session,
                prompt_version=L3_BRIEF.version,
                json_model=BriefDraft,
            )
            return parse_structured(completion.text, validator=lambda d: BriefDraft(**d))
        except JsonParseError as exc:
            logger.warning("l3 brief draft failed: %s", exc)
            return None