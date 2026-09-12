"""L2 隐性冲突检测编排（系统设计 §5.2 / 架构设计 §7.2 多层候选漏斗）。

漏斗（P0 落地形态，向量近邻 L2-2 留待 pgvector 阶段）：
  L2-1 增量筛选   只对 `claims_scanned_at IS NULL` 或落后于 `updated_at` 的条目
                  （重）提取主张；主张是冲突判定的最小比对粒度（ADR-09）
  L2-3 规则预筛   同 topic 才组对（同主题预筛索引 ix_claims_user_topic）
  L2-4 立场/强度  极性相反、强度高的候选对优先送判
  L2-5 LLM 判定   conflict_detection（reasoning=on），ADR-11 JSON Schema：
                  relation（矛盾/互补/断层/无关）+ evidence + confidence，
                  解析失败回退本地 JSON 解析（与 L1 同款顽健路径）
  L2-6 阈值+抑制  confidence < l2_min_confidence 丢弃（架构 §7.3）；
                  同类型冲突被用户忽略 ≥N 次后不再产生该类推荐（UC-L2-03）

幂等（ADR-13）：pair_key（无序主张对）已存在的对永不重复判定入库。
可靠性：主张提取失败不写 claims_scanned_at（下次扫描自动重试）；
LLM 调用绝不压在写事务里——每完成一个条目/一条冲突立即 commit 释放 SQLite 写锁。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.domain.models.claim import Claim
from app.domain.models.conflict import Conflict
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import ConflictRepository, make_pair_key
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.gateway import ModelGateway
from app.llm.structure import JsonParseError, parse_structured

logger = logging.getLogger(__name__)

_MAX_CLAIM_STATEMENT = 500


def _utcnow() -> datetime:
    """naive UTC：与 SQLite server_default(func.now()) 的存储形态一致，
    避免 aware/naive datetime 比较抛 TypeError。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---- LLM 输出 Schema（ADR-11）---------------------------------------------


class ExtractedClaim(BaseModel):
    """单条主张：归一化、可独立比对的原子观点。"""

    statement: str = Field(min_length=1, max_length=_MAX_CLAIM_STATEMENT)
    topic: str = Field(default="", max_length=64)
    polarity: int = Field(default=0, ge=-1, le=1)  # -1 否定 / 0 中性 / 1 肯定
    strength: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    claims: list[ExtractedClaim] = Field(default_factory=list)


class ConflictJudgment(BaseModel):
    """L2-5 结构化判定输出（架构 §7.2：relation + 证据 + 置信度）。"""

    relation: Literal["矛盾", "互补", "断层", "无关"] = "无关"
    conflict_type: str = Field(default="", max_length=32)  # 矛盾时的细分类型，如"立场对立"
    detail: str = ""
    suggestion: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


# ---- 提示词 ----------------------------------------------------------------

_EXTRACT_SYS = (
    "你是主张提取器。从知识条目中提取核心主张：可独立比对、不含上下文也能读懂的原子观点。"
    "输出严格 JSON："
    '{"claims":[{"statement":"...","topic":"2-6字主题标签","polarity":-1|0|1,'
    '"strength":0..1,"confidence":0..1}]}。'
    "statement 用陈述句归一化表述；只提取观点性内容，事实性背景不提；"
    "没有可提取主张时输出 {\"claims\":[]}。"
)

_JUDGE_SYS = (
    "你是观点冲突判定器。判断两条主张的关系，输出严格 JSON："
    '{"relation":"矛盾|互补|断层|无关","conflict_type":"","detail":"","suggestion":"","confidence":0..1}。'
    "relation 定义：矛盾=对同一问题的立场不可兼容；互补=视角不同但可并存；"
    "断层=话题相邻但关注点错开；无关=仅主题词相近。"
    "relation=矛盾 时必须给 conflict_type（2-6 字，如：立场对立/前提冲突/结论互斥/方法冲突）"
    "并在 detail 中引用双方原句作为证据；suggestion 给一个可执行的动作建议。"
    "宁可判互补/断层也不要把分歧夸大成矛盾。"
)


# ---- 结果结构 --------------------------------------------------------------


@dataclass
class PairOutcome:
    claim_a_id: str
    claim_b_id: str
    relation: str
    created: bool
    suppressed: bool = False
    reason: str = ""


@dataclass
class L2ScanResult:
    scanned_items: int = 0           # 本轮完成主张提取的条目数
    claims_extracted: int = 0        # 新入库主张数
    extraction_failures: int = 0     # 提取失败（留待下轮重试）
    pairs_judged: int = 0            # 成功拿到 LLM 判定的对数（解析失败不计入）
    conflicts_found: int = 0         # 新入库冲突数
    conflicts_suppressed: int = 0    # 因反馈抑制未入库数
    conflict_ids: list[str] = field(default_factory=list)


class L2Orchestrator:
    def __init__(self, gateway: ModelGateway, session: Session):
        self._gateway = gateway
        self._session = session

    # ---- 对外入口 ------------------------------------------------------

    def scan(self, *, user_id: str) -> L2ScanResult:
        result = L2ScanResult()
        krepo = KnowledgeRepository(self._session, user_id=user_id)
        crepo = ClaimRepository(self._session, user_id=user_id)
        xrepo = ConflictRepository(self._session, user_id=user_id)

        items = {i.id: i for i in krepo.list_active(user_id)}

        # L2-1：增量主张提取（失败不置位，下轮重试；成功立即 commit 释放写锁）
        for item in items.values():
            if not self._needs_rescan(item):
                continue
            claims = self._extract_claims(item, user_id=user_id)
            if claims is None:
                result.extraction_failures += 1
                continue
            crepo.delete_by_item(item.id)  # 重扫 = 替换旧主张
            created = 0
            for c in claims[: settings.l2_max_claims_per_item]:
                crepo.create(
                    user_id=user_id,
                    knowledge_item_id=item.id,
                    statement=c.statement[:_MAX_CLAIM_STATEMENT],
                    topic=c.topic.strip()[:64],
                    polarity=c.polarity,
                    strength=c.strength,
                    confidence=c.confidence,
                )
                created += 1
            item.claims_scanned_at = _utcnow()
            self._session.commit()
            result.scanned_items += 1
            result.claims_extracted += created

        if not items:
            return result

        # L2-2~4：候选对生成（同 topic 预筛 + 立场/强度排序 + 上限闸门）
        # L2-6 前置：被用户反复忽略的冲突类型直接收敛（UC-L2-03）
        suppressed_types = self._suppressed_types(user_id, xrepo)
        all_claims = [c for c in crepo.list_by_user(user_id) if c.knowledge_item_id in items]
        pairs = self._candidate_pairs(all_claims)

        for claim_a, claim_b in pairs:
            pair_key = make_pair_key(claim_a.id, claim_b.id)
            if xrepo.find_by_pair(user_id, pair_key) is not None:
                continue  # ADR-13 幂等：同对主张只判一次

            judgment = self._judge_pair(claim_a, claim_b, items, user_id=user_id)
            if judgment is None:
                continue  # LLM 失败：不入库，下轮重试
            result.pairs_judged += 1

            if judgment.relation != "矛盾":
                continue  # 互补/断层/无关：仅记录日志，不落库
            if judgment.confidence < settings.l2_min_confidence:
                logger.info(
                    "l2 conflict dropped (low confidence %.2f) pair=%s",
                    judgment.confidence, pair_key,
                )
                continue

            if judgment.conflict_type in suppressed_types:
                result.conflicts_suppressed += 1
                logger.info("l2 conflict suppressed type=%s pair=%s", judgment.conflict_type, pair_key)
                continue

            conflict = xrepo.create(
                user_id=user_id,
                item_a_id=claim_a.knowledge_item_id,
                item_b_id=claim_b.knowledge_item_id,
                claim_a_id=claim_a.id,
                claim_b_id=claim_b.id,
                conflict_type=judgment.conflict_type or "矛盾",
                detail=judgment.detail,
                suggestion=judgment.suggestion,
                confidence=judgment.confidence,
            )
            self._session.commit()  # 立即提交，写锁不跨 LLM 调用
            result.conflicts_found += 1
            result.conflict_ids.append(conflict.id)
            logger.info(
                "l2 conflict created type=%s conf=%.2f pair=%s",
                judgment.conflict_type, judgment.confidence, pair_key,
            )

        return result

    # ---- L2-1 主张提取 ---------------------------------------------------

    @staticmethod
    def _needs_rescan(item: KnowledgeItem) -> bool:
        """是否需要（重新）提取主张。

        语义：claims_scanned_at 为 NULL 即待扫。条目文本变更时由
        KnowledgeRepository.apply_update 把该列置回 NULL（主张已过期），
        因此无需跨时钟比较 Python/数据库两个时间源——那类比较会被
        亚秒精度差与 onupdate 二次写时间戳搞得不可靠。
        """
        return item.claims_scanned_at is None

    def _extract_claims(self, item: KnowledgeItem, *, user_id: str) -> list[ExtractedClaim] | None:
        """提取失败返回 None（调用方跳过置位，下轮自动重试）。"""
        messages = [
            {"role": "system", "content": _EXTRACT_SYS},
            {
                "role": "user",
                "content": f"标题：{item.title}\n\n正文：\n{(item.raw_content or '')[:4000]}",
            },
        ]
        try:
            completion = self._gateway.chat(
                task_type="batch_extraction", messages=messages, user_id=user_id, session=self._session
            )
            data = parse_structured(completion.text, validator=lambda d: ExtractionResult(**d))
        except JsonParseError as exc:
            logger.warning("l2 claim extraction failed item=%s: %s", item.id, exc)
            return None
        return data.claims

    # ---- L2-2~4 候选对生成 -------------------------------------------------

    @staticmethod
    def _candidate_pairs(claims: list[Claim]) -> list[tuple[Claim, Claim]]:
        """同 topic 组对 + 立场/强度排序 + 单次上限闸门（ADR-12 禁止全量两两比对）。"""
        by_topic: dict[str, list[Claim]] = {}
        for claim in claims:
            topic = (claim.topic or "").strip()
            if topic:
                by_topic.setdefault(topic, []).append(claim)

        pairs: list[tuple[Claim, Claim]] = []
        for bucket in by_topic.values():
            if len(bucket) < 2:
                continue
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    a, b = bucket[i], bucket[j]
                    if a.knowledge_item_id == b.knowledge_item_id:
                        continue  # 同条目内部的自洽性不是跨知识冲突
                    pairs.append((a, b))

        def priority(pair: tuple[Claim, Claim]) -> float:
            a, b = pair
            opposite = 2.0 if a.polarity * b.polarity == -1 else 0.0
            return opposite + (a.strength + b.strength) / 2

        pairs.sort(key=priority, reverse=True)
        return pairs[: settings.l2_max_pairs_per_scan]

    # ---- L2-6 反馈抑制 -----------------------------------------------------

    @staticmethod
    def _suppressed_types(user_id: str, xrepo: ConflictRepository) -> set[str]:
        """被忽略 ≥N 次的冲突类型不再产生推荐（误报抑制）。"""
        counts = xrepo.ignored_type_counts(user_id)
        return {
            t for t, n in counts.items() if n >= settings.l2_ignore_suppress_threshold and t
        }

    # ---- L2-5 LLM 判定 ------------------------------------------------------

    def _judge_pair(
        self,
        claim_a: Claim,
        claim_b: Claim,
        items: dict[str, KnowledgeItem],
        *,
        user_id: str,
    ) -> ConflictJudgment | None:
        item_a = items.get(claim_a.knowledge_item_id)
        item_b = items.get(claim_b.knowledge_item_id)
        messages = [
            {"role": "system", "content": _JUDGE_SYS},
            {
                "role": "user",
                "content": (
                    f"主张A（来自《{item_a.title if item_a else '未知条目'}》）：{claim_a.statement}\n"
                    f"主张B（来自《{item_b.title if item_b else '未知条目'}》）：{claim_b.statement}\n\n"
                    "请判定两条主张的关系并输出 JSON。"
                ),
            },
        ]
        try:
            completion = self._gateway.chat(
                task_type="conflict_detection", messages=messages, user_id=user_id, session=self._session
            )
            return parse_structured(completion.text, validator=lambda d: ConflictJudgment(**d))
        except JsonParseError as exc:
            logger.warning("l2 judgment failed pair=%s|%s: %s", claim_a.id, claim_b.id, exc)
            return None