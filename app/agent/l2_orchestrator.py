"""L2 隐性冲突检测编排（系统设计 §5.2 / 架构设计 §7.2 多层候选漏斗）。

漏斗（P0 落地形态）：
  L2-1 增量筛选   只对 `claims_scanned_at IS NULL` 或落后于 `updated_at` 的条目
                  （重）提取主张；主张是冲突判定的最小比对粒度（ADR-09）。
                  **书籍通读笔记不经过提取**：分章要点已是主张形态，直接以
                  「影子主张」入漏斗（`knowledge_item_id="book:<book_id>"`），
                  跳过一次提取调用——见 `_sync_book_claims`
  L2-2 语义近邻   主张向量余弦落在 [sim_lo, sim_hi] 带内即组对，兜住 topic 标签措辞
                  不稳定导致的漏召回；向量优先读 `Claim.embedding`，缺失才补算并回写
                  （见 `_claim_vectors`）——两侧文本口径由 `_claim_embed_text` 唯一收口
  L2-3 规则预筛   同 topic 才组对（同主题预筛索引 ix_claims_user_topic）
  L2-4 立场/强度  极性相反、强度高的候选对优先送判
  L2-5 LLM 判定   conflict_detection（reasoning=on），ADR-11 JSON Schema：
                  relation（矛盾/互补/断层/无关）+ evidence + confidence，
                  解析失败回退本地 JSON 解析（与 L1 同款顽健路径）
  L2-6 阈值+抑制  confidence < l2_min_confidence 丢弃（架构 §7.3）；
                  同类型冲突被用户忽略 ≥N 次后不再产生该类推荐（UC-L2-03）
  L2-7 复核合议   低置信「矛盾」（校准值 ∈ [review_lo, review_hi]）换角度重判一次
                  （conflict_review），规则合议定终判——同判维持加成、分歧推翻压低；
                  预算硬闸门（l2_review_max_per_scan），欠账标 pending 下轮先补

幂等（ADR-13）：pair_key（无序主张对）已判定过的对永不重复送判——
冲突表挡重复入库，判定日志挡重复**判定**（非矛盾对不产生冲突行，
只查冲突表会让同一对每轮重判烧钱）。翻案走 rejudge 受控通道。
可靠性：主张提取失败不写 claims_scanned_at（下次扫描自动重试）；
LLM 调用绝不压在写事务里——每完成一个条目/一条冲突立即 commit 释放 SQLite 写锁。

**素材边界（2026-09-28 扩展）**：知识条目（手动 / 划词 / 微信读书）+ 智能体通读笔记。
书籍要点以影子主张参与「书 vs 笔记」「书 vs 书」的冲撞——外部知识冲撞已知，
正是本能力存在的理由；对话闲聊、目标冲突不在此列（分别归 chat 与 L4）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agent.l2_judge_quality import (
    FinalVerdict,
    PairSignals,
    calibrate_l2_confidence,
    cites_evidence,
    merge_review,
    needs_review,
)
from app.core.config import settings
from app.domain.models.claim import Claim
from app.domain.models.conflict import Conflict
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.repositories.claim_repository import ClaimRepository
from app.domain.repositories.conflict_repository import (
    VALID_STATES,
    ConflictRepository,
    make_pair_key,
)
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from app.llm.exceptions import LLMError
from app.llm.gateway import ModelGateway
from app.llm.prompts import L2_EXTRACT, L2_JUDGE, L2_REVIEW
from app.llm.structure import JsonParseError, parse_structured
from app.retrieval.embedding import EmbeddingModel, build_embedding
from app.retrieval.vector_store import cos_sim

logger = logging.getLogger(__name__)

_MAX_CLAIM_STATEMENT = 500

# 书籍影子主张的合成来源键前缀：Claim/Conflict 的 knowledge_item_id / item_*_id
# 填 `book:<book_id>`。以 "book:" 开头即「这条主张来自智能体通读笔记」，
# 展示层据此改查 Book 表取标题（见 conflict_view）。**:安全: 前缀含冒号，与 UUID 天然不相交。
_BOOK_SOURCE_PREFIX = "book:"

# 每份通读笔记最多贡献的影子主张数：分章要点取 gist + 每章前 2 条要点。
# 不设上限的话，一本 24 块的书 × 每块 5 要点会单独吃掉整轮配对预算。
_MAX_CLAIMS_PER_READING = 40

# 每轮扫描随卡展示的「印证」上限：矛盾才是本能力的主产出，印证是补充
_MAX_ECHOES = 3

# 无观点文本标记：封面 / 目录 / 版权页 / 残句在通读笔记里也会产出「要点」，
# 它们没有可比对的观点，放进漏斗只会白占判定预算（真书实测踩到）。
_NOISE_MARKERS = (
    "原文仅显示", "无可供提炼", "无法提炼", "文本残缺", "无法概括",
    "未包含任何正文", "无实质内容", "未包含实质内容", "版权", "目录页",
    "导航性栏目", "只列出了", "仅显示「",
)


def _is_noisy_viewpoint(text: str) -> bool:
    return any(marker in text for marker in _NOISE_MARKERS)


def book_source_key(book_id: str) -> str:
    """书籍影子主张的来源键（Claim.knowledge_item_id / Conflict.item_*_id 共用）。"""
    return f"{_BOOK_SOURCE_PREFIX}{book_id}"


def is_book_source(source_id: str | None) -> bool:
    return bool(source_id) and str(source_id).startswith(_BOOK_SOURCE_PREFIX)


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


class ReviewJudgment(ConflictJudgment):
    """L2-7 复核裁判输出：在初判 schema 上加自辩两栏（一次调用内先自辩再终判）。"""

    uphold_reason: str = ""
    overturn_reason: str = ""


# ---- 提示词 ----------------------------------------------------------------

# 提示词统一在 app/llm/prompts.py 声明（版本化 + golden set 校验），此处仅取别名
_EXTRACT_SYS = L2_EXTRACT.text
_JUDGE_SYS = L2_JUDGE.text
_REVIEW_SYS = L2_REVIEW.text


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
    book_readings_used: int = 0      # 参与本轮的通读笔记份数
    # 本轮发现的「印证」（书观点 ↔ 笔记的互补对，跨来源、最多 3 条）：
    # 矛盾逼你修正，印证给你确认——两者都是书与笔记冲撞的产出
    echoes: list[dict] = field(default_factory=list)
    conflict_ids: list[str] = field(default_factory=list)
    reviews_run: int = 0             # 本轮实际发出的复核调用数（含解析失败的，预算按次计）
    reviews_overturned: int = 0      # 复核推翻初判的对数（翻案/拦下都算推翻）
    reviews_pending: int = 0         # 本轮结束仍欠复核的对数（预算耗尽或复核失败）


@dataclass
class RejudgeResult:
    """翻案终判的落点信息（供 API 呈现「冲突现在活没活」）。"""

    verdict: FinalVerdict
    conflict_id: str | None = None   # 终判为矛盾时的冲突 id（upsert 后）
    conflict_active: bool = False    # 终判为矛盾且已入库/存活
    retracted: bool = False          # 终判非矛盾且撤回了原有冲突


class L2Orchestrator:
    def __init__(
        self,
        gateway: ModelGateway,
        session: Session,
        embedding: EmbeddingModel | None = None,
    ):
        self._gateway = gateway
        self._session = session
        self._embedding = embedding or build_embedding()

    # ---- 对外入口 ------------------------------------------------------

    def scan(self, *, user_id: str) -> L2ScanResult:
        result = L2ScanResult()
        krepo = KnowledgeRepository(self._session, user_id=user_id)
        crepo = ClaimRepository(self._session, user_id=user_id)
        xrepo = ConflictRepository(self._session, user_id=user_id)
        from app.domain.repositories.l2_judgment_log_repository import L2JudgmentLogRepository

        log_repo = L2JudgmentLogRepository(self._session, user_id=user_id)

        items = {i.id: i for i in krepo.list_active(user_id)}

        # 书籍通读笔记 → 影子主张（要点即主张，跳过提取；增量口径见函数内注释）。
        # 返回的标题映射供判定提示词使用：书观点以《书名》的语境送进判定。
        book_titles = self._sync_book_claims(user_id, crepo)

        # L2-1：增量主张提取（失败不置位，下轮重试；成功立即 commit 释放写锁）
        for item in items.values():
            if not self._needs_rescan(item):
                continue
            claims = self._extract_claims(item, user_id=user_id)
            if claims is None:
                result.extraction_failures += 1
                continue
            crepo.delete_by_item(item.id)  # 重扫 = 替换旧主张
            picked = claims[: settings.l2_max_claims_per_item]
            # 嵌入文本与落库 statement 用**同一个** `_claim_embed_text` 产出：
            # 相似度侧是拿库里的 statement 重新送入同一口径算的，两者必须逐字相同，
            # 否则「提取时存的向量」与「检索时算的向量」落在不同文本上，余弦失去意义。
            texts = [self._claim_embed_text(c.statement) for c in picked]
            # 主张向量化：持久化到 Claim.embedding（供后续向量近邻检索复用）。
            # 失败只跳过向量、不阻断主张落库（可靠-4）。
            vecs = self._embed_texts(texts)
            created = 0
            for idx, c in enumerate(picked):
                crepo.create(
                    user_id=user_id,
                    knowledge_item_id=item.id,
                    statement=texts[idx],
                    topic=c.topic.strip()[:64],
                    polarity=c.polarity,
                    strength=c.strength,
                    confidence=c.confidence,
                    embedding=EmbeddingModel.dumps(vecs[idx]) if vecs else None,
                )
                created += 1
            item.claims_scanned_at = _utcnow()
            self._session.commit()
            result.scanned_items += 1
            result.claims_extracted += created

        # 判定提示词的标题语境：知识条目用条目标题，书籍影子主张用《书名》
        titles = {item_id: item.title for item_id, item in items.items()}
        titles.update(book_titles)

        if not titles:
            return result

        # L2-2~4：候选对生成（同 topic 预筛 + 立场/强度排序 + 上限闸门）
        # L2-6 前置：被用户反复忽略的冲突类型直接收敛（UC-L2-03）
        suppressed_types = self._suppressed_types(user_id, xrepo)
        valid_sources = set(titles)
        all_claims = [c for c in crepo.list_by_user(user_id) if c.knowledge_item_id in valid_sources]
        result.book_readings_used = len(book_titles)
        pairs = self._candidate_pairs(all_claims)

        # 待复核候选（二段队列）：(距决策边界距离, 原判行, 初判建议)。
        # 候选暂缓应用终判——矛盾不入不丢，等复核合议后统一走 `_apply_verdict` 收口。
        review_candidates: list[tuple[float, object, str]] = []

        for claim_a, claim_b, sim in pairs:
            pair_key = make_pair_key(claim_a.id, claim_b.id)
            if xrepo.find_by_pair(user_id, pair_key) is not None:
                continue  # ADR-13 幂等：同对主张只判一次（已有冲突不再判）
            # 判定日志即幂等账本：判成**非矛盾**的对不产生冲突行，若只查冲突表，
            # 同一对稳定主张每轮扫描都会被重新送判烧钱（日志重复膨胀）。
            # 已有判定记录（终判或 pending 待复核）都不再重判；翻案走受控通道。
            if log_repo.exists_pair(user_id, pair_key):
                continue

            judgment = self._judge_pair(claim_a, claim_b, titles, user_id=user_id)
            if judgment is None:
                continue  # LLM 失败：不入库，下轮重试
            result.pairs_judged += 1

            # 置信度校准（判断层，ADR-15 范式）：不信任模型裸自报，用本地信号
            # 缩放+微调。阈值判校准值——「模型自信」与「证据充分」分开把关。
            signals = self._pair_signals(claim_a, claim_b, judgment, sim=sim)
            calibrated = calibrate_l2_confidence(judgment.confidence, signals)

            # 判定明细落库（**含非矛盾**）：排查「为什么没抓出冲突」与成本审计的
            # 事实来源。自包含（标题与文本随行）——影子主张会被重扫替换，
            # 靠 claim_id 回查会静默丢文本。写入后立即 commit，写锁不跨 LLM 调用。
            log_row = log_repo.create(
                user_id=user_id,
                pair_key=pair_key,
                claim_a_id=claim_a.id,
                claim_b_id=claim_b.id,
                source_a=claim_a.knowledge_item_id,
                source_b=claim_b.knowledge_item_id,
                title_a=titles.get(claim_a.knowledge_item_id, ""),
                title_b=titles.get(claim_b.knowledge_item_id, ""),
                claim_a_text=claim_a.statement,
                claim_b_text=claim_b.statement,
                relation=judgment.relation,
                conflict_type=judgment.conflict_type,
                confidence=judgment.confidence,
                calibrated_confidence=calibrated,
                sim=sim,
                polarity_a=claim_a.polarity,
                polarity_b=claim_b.polarity,
                detail=judgment.detail,
            )
            self._session.commit()

            if needs_review(
                relation=judgment.relation,
                calibrated=calibrated,
                enabled=settings.l2_review_enabled,
                lo=settings.l2_review_lo,
                hi=settings.l2_review_hi,
            ):
                # 待复核：本判暂缓应用（矛盾不入不丢），进二段队列按预算复核。
                # 原判行自包含（文本/来源/极性全在），复核只依赖它。
                review_candidates.append(
                    (abs(calibrated - settings.l2_min_confidence), log_row, judgment.suggestion)
                )
                continue

            self._apply_verdict(
                user_id=user_id, result=result, xrepo=xrepo,
                suppressed_types=suppressed_types, pair_key=pair_key,
                source_a=claim_a.knowledge_item_id, source_b=claim_b.knowledge_item_id,
                claim_a_id=claim_a.id, claim_b_id=claim_b.id,
                title_a=titles.get(claim_a.knowledge_item_id, ""),
                title_b=titles.get(claim_b.knowledge_item_id, ""),
                claim_a_text=claim_a.statement, claim_b_text=claim_b.statement,
                relation=judgment.relation, conflict_type=judgment.conflict_type,
                detail=judgment.detail, suggestion=judgment.suggestion,
                confidence=calibrated,
            )

        # 二段复核：先补上轮欠账（pending 队列），再按「距决策边界越近越优先」
        # 处理本轮候选。预算硬闸门；超出的标 pending，下轮扫描补跑。
        self._run_reviews(
            user_id=user_id, result=result, xrepo=xrepo, log_repo=log_repo,
            suppressed_types=suppressed_types,
            candidates=sorted(review_candidates, key=lambda t: t[0]),
        )

        # 行为埋点：扫描产出的规模指标，供 L4/L5 与成本分析使用（不记冲突正文）
        from app.feedback import events

        events.record(
            self._session, user_id=user_id, event_type=events.L2_SCAN,
            payload={
                "scanned_items": result.scanned_items,
                "claims_extracted": result.claims_extracted,
                "pairs_judged": result.pairs_judged,
                "conflicts_found": result.conflicts_found,
                "conflicts_suppressed": result.conflicts_suppressed,
                "extraction_failures": result.extraction_failures,
                "book_readings_used": result.book_readings_used,
                "echoes_found": len(result.echoes),
                "reviews_run": result.reviews_run,
                "reviews_overturned": result.reviews_overturned,
                "reviews_pending": result.reviews_pending,
            },
        )
        return result

    # ---- 用户反馈 ------------------------------------------------------

    @staticmethod
    def decide_conflict(session: Session, *, user_id: str, conflict_id: str, state: str):
        """用户对一条冲突的反馈（unseen / ignored / accepted）。

        返回冲突实体；冲突不存在返回 None；状态非法抛 ValueError；
        冲突属于他人则让仓储的 PermissionError 原样抛出（由调用方决定呈现方式）。

        放在编排层而不是端点里，是因为它有两条调用路径——原页面端点与对话卡片的
        操作按钮。反馈语义（尤其是那条 `L2_CONFLICT_FEEDBACK` 事件，它是误报抑制
        和 L5 判断「用户是否采纳建议」的输入）只能有一份实现。
        """
        from app.feedback import events

        if state not in VALID_STATES:
            raise ValueError(f"非法冲突状态：{state}")

        xrepo = ConflictRepository(session, user_id=user_id)
        conflict = xrepo.get(conflict_id)
        if conflict is None:
            return None

        previous_state = conflict.user_state
        xrepo.set_state(conflict, state)
        session.commit()
        events.record(
            session, user_id=user_id, event_type=events.L2_CONFLICT_FEEDBACK,
            payload={
                "conflict_id": conflict.id,
                "from_state": previous_state,
                "to_state": state,
                "conflict_type": conflict.conflict_type,
                "confidence": conflict.confidence,
            },
        )
        return conflict

    def rejudge_pair(self, *, user_id: str, pair_key: str) -> RejudgeResult | None:
        """翻案通道（受控打破 pair_key 永久幂等）：强制完整重走 初判→校准→复核→合议。

        素材取判定日志的**自包含快照**（文本/标题/极性/来源全在行里）——claim_id
        回查会被重扫替换静默丢文本。终判收口复用 `_apply_verdict`：终判矛盾 →
        upsert（复活被撤回的旧冲突）；终判非矛盾 → 撤回已入库冲突。
        用户显式翻案不做推荐抑制（suppressed_types 传空）——这是他的直接指令，
        不是系统推荐。

        返回 None = LLM 判定失败（调用方 502）；该对无判定记录抛 LookupError（404）。
        复核失败照旧标 pending 并先按初判落地——pending 队列自愈，无需用户重试。
        """
        from app.domain.repositories.l2_judgment_log_repository import L2JudgmentLogRepository

        xrepo = ConflictRepository(self._session, user_id=user_id)
        log_repo = L2JudgmentLogRepository(self._session, user_id=user_id)
        row = log_repo.initial_for_pair(user_id, pair_key)
        if row is None:
            raise LookupError(f"无该对判定记录：{pair_key}")

        # 日志快照伪装成 Claim 喂给既有判定/信号管线（只用 id/文本/来源/极性四个字段）
        claim_a = SimpleNamespace(
            id=row.claim_a_id, statement=row.claim_a_text,
            knowledge_item_id=row.source_a, polarity=row.polarity_a,
        )
        claim_b = SimpleNamespace(
            id=row.claim_b_id, statement=row.claim_b_text,
            knowledge_item_id=row.source_b, polarity=row.polarity_b,
        )
        titles = {row.source_a: row.title_a, row.source_b: row.title_b}

        judgment = self._judge_pair(claim_a, claim_b, titles, user_id=user_id)
        if judgment is None:
            return None
        signals = self._pair_signals(claim_a, claim_b, judgment, sim=row.sim)
        calibrated = calibrate_l2_confidence(judgment.confidence, signals)

        # 重判是新的判定事实，append-only 写新原判行（旧两行保留可追溯）
        new_row = log_repo.create(
            user_id=user_id, pair_key=pair_key,
            claim_a_id=row.claim_a_id, claim_b_id=row.claim_b_id,
            source_a=row.source_a, source_b=row.source_b,
            title_a=row.title_a, title_b=row.title_b,
            claim_a_text=row.claim_a_text, claim_b_text=row.claim_b_text,
            relation=judgment.relation, conflict_type=judgment.conflict_type,
            confidence=judgment.confidence, calibrated_confidence=calibrated,
            sim=row.sim, polarity_a=row.polarity_a, polarity_b=row.polarity_b,
            detail=judgment.detail,
        )
        self._session.commit()

        result = L2ScanResult()  # 复用冲突计数通道拿 conflict_id
        suppressed: set[str] = set()  # 显式翻案不受推荐抑制
        had_conflict = xrepo.find_by_pair(user_id, pair_key) is not None

        if needs_review(
            relation=judgment.relation, calibrated=calibrated,
            enabled=settings.l2_review_enabled, lo=settings.l2_review_lo, hi=settings.l2_review_hi,
        ):
            verdict = self._review_pair(
                user_id=user_id, log_row=new_row, log_repo=log_repo, xrepo=xrepo,
                result=result, suppressed_types=suppressed,
                initial_suggestion=judgment.suggestion,
            )
            if verdict is not None:
                return RejudgeResult(
                    verdict=verdict,
                    conflict_id=result.conflict_ids[0] if result.conflict_ids else None,
                    conflict_active=bool(result.conflict_ids),
                    retracted=had_conflict and verdict.relation != "矛盾",
                )
            # 复核失败已标 pending：先按初判落地，欠账由扫描的 pending 队列自愈

        self._apply_verdict(
            user_id=user_id, result=result, xrepo=xrepo,
            suppressed_types=suppressed, pair_key=pair_key,
            source_a=row.source_a, source_b=row.source_b,
            claim_a_id=row.claim_a_id, claim_b_id=row.claim_b_id,
            title_a=row.title_a, title_b=row.title_b,
            claim_a_text=row.claim_a_text, claim_b_text=row.claim_b_text,
            relation=judgment.relation, conflict_type=judgment.conflict_type,
            detail=judgment.detail, suggestion=judgment.suggestion,
            confidence=calibrated,
        )
        return RejudgeResult(
            verdict=FinalVerdict(
                relation=judgment.relation, conflict_type=judgment.conflict_type,
                detail=judgment.detail, suggestion=judgment.suggestion,
                confidence=calibrated,
                review_state="pending" if new_row.review_state == "pending" else "none",
                overturned=False,
            ),
            conflict_id=result.conflict_ids[0] if result.conflict_ids else None,
            conflict_active=bool(result.conflict_ids),
            retracted=had_conflict and judgment.relation != "矛盾",
        )

    # ---- L2-1 主张提取 ---------------------------------------------------

    def _sync_book_claims(self, user_id: str, crepo: ClaimRepository) -> dict[str, str]:
        """把通读笔记同步成影子主张，返回 {source_key: 《书名》通读笔记} 供判定语境用。

        **为什么跳过提取**：分章要点在通读时已按「主张形态」产出（gist + 原子要点），
        再送一次提取调用是花钱重复劳动；这里只做「要点 → Claim 行」的转换与向量化。

        增量口径：`reading.updated_at`（重读覆盖会刷新）大于该书影子主张的最新
        `created_at` 时重建（delete + create，天然幂等）。两侧都是 SQLite `now()`
        写出的 naive datetime，同源可比；同秒边界下「主张不落后于笔记」即视为新鲜，
        最坏情形只是多一次重建。
        """
        from sqlalchemy import select

        from app.domain.models.book import Book
        from app.domain.models.book_agent_reading import BookAgentReading

        readings = list(self._session.scalars(
            select(BookAgentReading).where(
                BookAgentReading.user_id == user_id,
                BookAgentReading.status == "done",
                BookAgentReading.is_deleted.is_(False),
            )
        ))
        if not readings:
            return {}

        books = {
            b.id: b
            for b in self._session.scalars(
                select(Book).where(Book.id.in_([r.book_id for r in readings]))
            )
        }

        titles: dict[str, str] = {}
        for reading in readings:
            book = books.get(reading.book_id)
            if book is None or book.is_deleted:
                continue
            source = book_source_key(book.id)
            display = f"《{book.title}》通读笔记"
            titles[source] = display

            if self._book_claims_fresh(crepo, source, reading):
                continue

            claims = self._reading_to_claims(reading, book_title=book.title)
            crepo.delete_by_item(source)
            for claim in claims:
                text = self._claim_embed_text(claim.statement)
                vecs = self._embed_texts([text])
                crepo.create(
                    user_id=user_id,
                    knowledge_item_id=source,
                    statement=text,
                    topic=claim.topic,
                    polarity=claim.polarity,
                    strength=claim.strength,
                    confidence=claim.confidence,
                    embedding=EmbeddingModel.dumps(vecs[0]) if vecs else None,
                )
            self._session.commit()
            logger.info(
                "l2 book claims synced book=%s claims=%d", book.id, len(claims)
            )
        return titles

    @staticmethod
    def _book_claims_fresh(crepo: ClaimRepository, source: str, reading) -> bool:
        """该书影子主张是否仍与通读笔记同版（不落后于笔记的最近更新）。"""
        from datetime import datetime

        latest = crepo.latest_created_at(source)
        if latest is None:
            return False
        updated = reading.updated_at or reading.created_at
        if not isinstance(updated, datetime):
            return False
        return latest >= updated.replace(tzinfo=None) if updated.tzinfo else latest >= updated

    @staticmethod
    def _reading_to_claims(reading, *, book_title: str) -> list[ExtractedClaim]:
        """通读笔记 → 影子主张：每章取要点（缺要点的章节用 gist 兜底），限量。

        topic 优先用通读时模型打的**领域标签**（BOOK_DIGEST_CHUNK v2 起）——
        它与笔记主张的主题标签出自同一套打标签逻辑，能走 topic 通道配对；
        旧笔记（v1 时期）没有标签则回退书名（此时书观点只能靠语义通道）。

        statement 保持纯观点文本（书名语境由判定提示词带出）——把它拼进 statement
        会污染 embedding 相似度：同一观点在不同书名前缀下算不出相近的向量。
        """
        claims: list[ExtractedClaim] = []
        fallback_topic = (book_title or "").strip()[:64]
        for chapter in reading.chapters_note or []:
            if not chapter.get("gist") or _is_noisy_viewpoint(chapter["gist"]):
                continue
            topic = (chapter.get("topic") or "").strip()[:64] or fallback_topic
            points = [str(p).strip() for p in (chapter.get("points") or []) if str(p).strip()]
            points = [p for p in points if not _is_noisy_viewpoint(p)]
            statements = points[:2] if points else [str(chapter.get("gist", "")).strip()]
            for statement in statements:
                if not statement or _is_noisy_viewpoint(statement):
                    continue
                claims.append(ExtractedClaim(
                    statement=statement[:_MAX_CLAIM_STATEMENT],
                    topic=topic,
                    polarity=0,
                    strength=0.5,
                    confidence=0.6,
                ))
                if len(claims) >= _MAX_CLAIMS_PER_READING:
                    return claims
        return claims

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
                task_type="batch_extraction", messages=messages, user_id=user_id, session=self._session,
                prompt_version=L2_EXTRACT.version, json_model=ExtractionResult,
            )
            data = parse_structured(completion.text, validator=lambda d: ExtractionResult(**d))
        except JsonParseError as exc:
            logger.warning("l2 claim extraction failed item=%s: %s", item.id, exc)
            return None
        return data.claims

    # ---- L2-2~4 候选对生成 -------------------------------------------------

    def _candidate_pairs(self, claims: list[Claim]) -> list[tuple[Claim, Claim, float | None]]:
        """候选对生成：topic 通道 ∪ 语义近邻通道，按立场/相似度排序，上限闸门。

        - topic 通道：归一化后同主题即组对（召回廉价但受 LLM 标签措辞影响——
          实测两次提取把同一主题标成「战略定力/快速调整」等互不相同的词）
        - 语义通道（L2-2）：主张向量余弦落在 [sim_lo, sim_hi] 带内才组对——
          太近≈重复表述、太远≈无关，两端都排除（架构 §7.2 要点）；
          向量取自 `Claim.embedding`（缺失才补算回写，见 `_claim_vectors`）
        - embedding 不可用时自动退化为纯 topic 通道，不阻断扫描
        - 返回 (a, b, sim)：sim 随对带走（置信度校准的信号），topic 通道无向量时 None
        """
        if not claims:
            return []
        sims = self._claim_similarities(claims)

        pairs: list[tuple[Claim, Claim, float]] = []
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                a, b = claims[i], claims[j]
                if a.knowledge_item_id == b.knowledge_item_id:
                    continue  # 同条目内部的自洽性不是跨知识冲突
                sim = sims.get((a.id, b.id))
                same_topic = (
                    (a.topic or "").strip() != ""
                    and (a.topic or "").strip() == (b.topic or "").strip()
                )
                if same_topic:
                    pairs.append((a, b, sim))
                elif sim is not None and settings.l2_pair_sim_lo <= sim <= settings.l2_pair_sim_hi:
                    pairs.append((a, b, sim))

        def priority(pair: tuple[Claim, Claim, float | None]) -> float:
            a, b, sim = pair
            sim = sim if sim is not None else 0.0
            opposite = 2.0 if a.polarity * b.polarity == -1 else 0.0
            # 跨来源（书观点 vs 笔记 / 书 vs 书）优先送判：书观点是新增信息，
            # 若不加权，topic 通道的书对会被「同主题笔记对」按 sim 挤出名额
            # （真书实测：书×笔 0/20 入选）。幂等保证每对只判一次，额度不失控。
            cross_source = (
                2.0
                if is_book_source(a.knowledge_item_id) != is_book_source(b.knowledge_item_id)
                or (is_book_source(a.knowledge_item_id) and is_book_source(b.knowledge_item_id))
                else 0.0
            )
            return opposite * 10 + cross_source * 10 + sim

        pairs.sort(key=priority, reverse=True)
        # 返回三元组带 sim：判定后的置信度校准要它做信号（topic 通道无向量时为 None）
        return [(a, b, sim) for a, b, sim in pairs[: settings.l2_max_pairs_per_scan]]

    @staticmethod
    def _claim_embed_text(statement: str | None) -> str:
        """主张送入 embedding 的文本口径（**唯一来源**）。

        扫描侧写库与相似度侧补算都必须经这里。历史上两侧各写各的
        （一侧 `statement[:500]`、另一侧裸 `statement`）——只要改一次常量就会静默分叉：
        已落库的向量与新算的向量落在不同文本上，余弦照旧算得出来，但已不是同一个东西。
        """
        return (statement or "").strip()[:_MAX_CLAIM_STATEMENT]

    def _claim_similarities(self, claims: list[Claim]) -> dict[tuple[str, str], float]:
        """主张两两余弦；任一方取不到向量就不产出该键（该对退化为 topic 通道）。"""
        if len(claims) < 2:
            return {}
        vectors = self._claim_vectors(claims)
        sims: dict[tuple[str, str], float] = {}
        for i in range(len(claims)):
            for j in range(i + 1, len(claims)):
                vi, vj = vectors[i], vectors[j]
                if vi is None or vj is None:
                    continue
                sims[(claims[i].id, claims[j].id)] = cos_sim(vi, vj)
        return sims

    def _claim_vectors(self, claims: list[Claim]) -> list[list[float] | None]:
        """按 `claims` 顺序取主张向量：优先读 `Claim.embedding`，缺失才补算并回写。

        为什么必须读库：主张是**存量数据**，而 L2 每轮扫描都要重算一遍两两余弦
        （`pair_key` 幂等只挡住重复**判定**，挡不住重复**向量化**）。若每轮现场 embed
        全部主张，等于把提取时已付过的向量成本按扫描次数重复支付，并随存量线性增长——
        主张把冲突检测从 O(n²) 降为近邻检索的收益会被二次抵消（ADR-09 的本意）。

        可降级：embedding 不可用时已读到的向量照旧可用，只有缺失位返回 None；
        位对里任一为 None 即不参与语义通道，不影响 topic 通道。
        """
        vectors = [self._load_claim_vector(c) for c in claims]
        missing = [i for i, v in enumerate(vectors) if v is None]
        if not missing:
            return vectors  # 全部命中：不产生任何写操作

        computed = self._embed_texts(
            [self._claim_embed_text(claims[i].statement) for i in missing]
        )
        if computed is None:
            return vectors  # 补算失败：保底用已读到的，不整体退化
        for i, vec in zip(missing, computed):
            vectors[i] = vec
            claims[i].embedding = EmbeddingModel.dumps(vec)  # 回写，下轮不再重算
        self._flush_claim_embeddings()
        return vectors

    def _load_claim_vector(self, claim: Claim) -> list[float] | None:
        """读一条已落库的主张向量；不可用（缺失 / 损坏 / 维度不符）返回 None。"""
        blob = claim.embedding
        if not blob:
            return None
        try:
            vec = [float(x) for x in EmbeddingModel.loads(blob)]
        except Exception as exc:  # noqa: BLE001 - 单条脏数据不该拖垮整轮扫描
            logger.warning("l2 claim embedding unreadable claim=%s: %s", claim.id, exc)
            return None

        dim = getattr(self._embedding, "dim", None)
        if isinstance(dim, int) and dim > 0 and len(vec) != dim:
            # 换过 embedding 模型（或历史脏数据）：旧向量与新向量不在同一空间。
            # 混用能算出数、但那个数没有意义——视为缺失，按当前模型补算覆盖。
            logger.info(
                "l2 claim embedding dim mismatch claim=%s stored=%d current=%d, recomputing",
                claim.id, len(vec), dim,
            )
            return None
        return vec

    def _flush_claim_embeddings(self) -> None:
        """回写补算出的主张向量；落库失败只降级为「下轮再补」，不影响本轮检索。

        这里用 commit 而非 flush：唯一调用点在 `scan` 的组对阶段，而该阶段之前
        每一步都已提交过，因此不存在「夹带未完成写操作」的情况。将来若把它挪到
        带挂起写的上下文里，那些写会被一并提交——需要改成局部事务。
        """
        try:
            self._session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("l2 claim embedding write-back failed: %s", exc)
            self._session.rollback()

    def _embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        """批量向量化文本；失败或返回条数不符时返回 None（调用方降级为无向量）。"""
        if not texts:
            return None
        try:
            vectors = list(self._embedding.embed(list(texts)))
        except Exception as exc:
            logger.warning("l2 claim embedding unavailable, topic-only pairing: %s", exc)
            return None
        if len(vectors) != len(texts):
            # 条数不符即与输入错位；按位回写会把向量写到错误的主张上——宁可不回写
            logger.warning(
                "l2 claim embedding size mismatch: expected %d got %d", len(texts), len(vectors)
            )
            return None
        return vectors

    # ---- L2-6 反馈抑制 -----------------------------------------------------

    @staticmethod
    def _suppressed_types(user_id: str, xrepo: ConflictRepository) -> set[str]:
        """被忽略 ≥N 次的冲突类型不再产生推荐（误报抑制）。"""
        counts = xrepo.ignored_type_counts(user_id)
        return {
            t for t, n in counts.items() if n >= settings.l2_ignore_suppress_threshold and t
        }

    # ---- L2-5 LLM 判定 ------------------------------------------------------

    @staticmethod
    def _pair_signals(
        claim_a: Claim, claim_b: Claim, judgment: ConflictJudgment, *, sim: float | None
    ) -> PairSignals:
        """送判对的本地信号快照（置信度校准用；全部离线可算，不问模型）。"""
        return PairSignals(
            relation=judgment.relation,
            polarity_opposite=claim_a.polarity * claim_b.polarity == -1,
            cross_source=(
                is_book_source(claim_a.knowledge_item_id)
                != is_book_source(claim_b.knowledge_item_id)
            ),
            sim=sim,
            text_a_len=len(claim_a.statement or ""),
            text_b_len=len(claim_b.statement or ""),
            evidence_cited=cites_evidence(
                judgment.detail, claim_a.statement, claim_b.statement
            ),
        )

    def _judge_pair(
        self,
        claim_a: Claim,
        claim_b: Claim,
        titles: dict[str, str],
        *,
        user_id: str,
    ) -> ConflictJudgment | None:
        title_a = titles.get(claim_a.knowledge_item_id, "未知条目")
        title_b = titles.get(claim_b.knowledge_item_id, "未知条目")
        messages = [
            {"role": "system", "content": _JUDGE_SYS},
            {
                "role": "user",
                "content": (
                    f"主张A（来自《{title_a}》）：{claim_a.statement}\n"
                    f"主张B（来自《{title_b}》）：{claim_b.statement}\n\n"
                    "请判定两条主张的关系并输出 JSON。"
                ),
            },
        ]
        try:
            completion = self._gateway.chat(
                task_type="conflict_detection", messages=messages, user_id=user_id, session=self._session,
                prompt_version=L2_JUDGE.version, json_model=ConflictJudgment,
            )
            return parse_structured(completion.text, validator=lambda d: ConflictJudgment(**d))
        except JsonParseError as exc:
            logger.warning("l2 judgment failed pair=%s|%s: %s", claim_a.id, claim_b.id, exc)
            return None

    # ---- L2-7 复核合议 ------------------------------------------------------

    def _apply_verdict(
        self,
        *,
        user_id: str,
        result: L2ScanResult,
        xrepo: ConflictRepository,
        suppressed_types: set[str],
        pair_key: str,
        source_a: str,
        source_b: str,
        claim_a_id: str,
        claim_b_id: str,
        title_a: str,
        title_b: str,
        claim_a_text: str,
        claim_b_text: str,
        relation: str,
        conflict_type: str,
        detail: str,
        suggestion: str,
        confidence: float,
    ) -> None:
        """终判的唯一入库收口（初判直过阈值 / 复核合议后共用）。

        非矛盾：印证展示；若该对曾有冲突（重判翻案场景）则撤回——终判说没有，
        库里就不该有。矛盾：阈值 + 抑制后 upsert（一 pair_key 一条冲突，
        被撤回的旧冲突复活而不是再插一行）。
        """
        if relation != "矛盾":
            existing = xrepo.find_by_pair(user_id, pair_key)
            if existing is not None:
                xrepo.retract(existing)
                self._session.commit()
                logger.info("l2 conflict retracted (verdict non-conflict) pair=%s", pair_key)
            # 印证：书观点与笔记的互补对（跨来源）——矛盾逼你修正，印证给你确认
            if (
                relation == "互补"
                and len(result.echoes) < _MAX_ECHOES
                and is_book_source(source_a) != is_book_source(source_b)
            ):
                result.echoes.append({
                    "title_a": title_a,
                    "title_b": title_b,
                    "claim_a": claim_a_text,
                    "claim_b": claim_b_text,
                })
            return

        if confidence < settings.l2_min_confidence:
            logger.info(
                "l2 conflict dropped (low confidence calibrated=%.2f) pair=%s",
                confidence, pair_key,
            )
            return
        if conflict_type in suppressed_types:
            result.conflicts_suppressed += 1
            logger.info("l2 conflict suppressed type=%s pair=%s", conflict_type, pair_key)
            return

        existing = xrepo.find_by_pair(user_id, pair_key, include_retracted=True)
        conflict = xrepo.upsert_from_rejudge(
            existing,
            user_id=user_id,
            item_a_id=source_a,
            item_b_id=source_b,
            claim_a_id=claim_a_id,
            claim_b_id=claim_b_id,
            conflict_type=conflict_type or "矛盾",
            detail=detail,
            suggestion=suggestion,
            confidence=confidence,
        )
        self._session.commit()  # 立即提交，写锁不跨 LLM 调用
        result.conflicts_found += 1
        result.conflict_ids.append(conflict.id)
        logger.info(
            "l2 conflict created type=%s conf=%.2f pair=%s",
            conflict_type, confidence, pair_key,
        )

    def _review_pair(
        self,
        *,
        user_id: str,
        log_row,
        log_repo,
        xrepo: ConflictRepository,
        result: L2ScanResult,
        suppressed_types: set[str],
        initial_suggestion: str = "",
    ) -> FinalVerdict | None:
        """换角度重判一次并合议（第二道闸，只花 1 次复核调用，不重判）。

        成功：写复核行（review_of_id 指向原判，日志 append-only）→ 合议终判 →
        按终判入库/拦下 → 原判行标 upheld/overturned → 埋事件。
        失败（LLM/解析）：原判行标 pending 返回 None——下轮扫描先补队列。
        """
        from app.feedback import events

        messages = [
            {"role": "system", "content": _REVIEW_SYS},
            {
                "role": "user",
                "content": (
                    f"主张A（来自《{log_row.title_a}》）：{log_row.claim_a_text}\n"
                    f"主张B（来自《{log_row.title_b}》）：{log_row.claim_b_text}\n\n"
                    f"初判：relation={log_row.relation}，conflict_type={log_row.conflict_type}，"
                    f"detail={log_row.detail}\n"
                    "请换角度重新审查并输出 JSON。"
                ),
            },
        ]
        # 预算按「发出的复核调用」计：解析失败也花了钱，必须占预算
        result.reviews_run += 1
        try:
            completion = self._gateway.chat(
                task_type="conflict_review", messages=messages, user_id=user_id, session=self._session,
                prompt_version=L2_REVIEW.version, json_model=ReviewJudgment,
            )
            review = parse_structured(completion.text, validator=lambda d: ReviewJudgment(**d))
        except (JsonParseError, LLMError) as exc:
            logger.warning("l2 review failed pair=%s: %s", log_row.pair_key, exc)
            log_repo.mark_review_state(log_row, "pending")
            self._session.commit()
            result.reviews_pending += 1
            return None

        review_signals = PairSignals(
            relation=review.relation,
            polarity_opposite=log_row.polarity_a * log_row.polarity_b == -1,
            cross_source=is_book_source(log_row.source_a) != is_book_source(log_row.source_b),
            sim=log_row.sim,
            text_a_len=len(log_row.claim_a_text or ""),
            text_b_len=len(log_row.claim_b_text or ""),
            evidence_cited=cites_evidence(review.detail, log_row.claim_a_text, log_row.claim_b_text),
        )
        review_calibrated = calibrate_l2_confidence(review.confidence, review_signals)

        verdict = merge_review(
            log_row.relation, log_row.conflict_type, log_row.detail,
            initial_suggestion, log_row.calibrated_confidence,
            review_relation=review.relation, review_type=review.conflict_type,
            review_detail=review.detail, review_suggestion=review.suggestion,
            review_calibrated=review_calibrated,
        )
        log_repo.create(
            user_id=user_id,
            pair_key=log_row.pair_key,
            claim_a_id=log_row.claim_a_id,
            claim_b_id=log_row.claim_b_id,
            source_a=log_row.source_a,
            source_b=log_row.source_b,
            title_a=log_row.title_a,
            title_b=log_row.title_b,
            claim_a_text=log_row.claim_a_text,
            claim_b_text=log_row.claim_b_text,
            relation=review.relation,
            conflict_type=review.conflict_type,
            confidence=review.confidence,
            calibrated_confidence=review_calibrated,
            sim=log_row.sim,
            review_of_id=log_row.id,
            polarity_a=log_row.polarity_a,
            polarity_b=log_row.polarity_b,
            detail=review.detail,
        )
        log_repo.mark_review_state(log_row, verdict.review_state)
        self._session.commit()
        if verdict.overturned:
            result.reviews_overturned += 1

        self._apply_verdict(
            user_id=user_id, result=result, xrepo=xrepo,
            suppressed_types=suppressed_types, pair_key=log_row.pair_key,
            source_a=log_row.source_a, source_b=log_row.source_b,
            claim_a_id=log_row.claim_a_id, claim_b_id=log_row.claim_b_id,
            title_a=log_row.title_a, title_b=log_row.title_b,
            claim_a_text=log_row.claim_a_text, claim_b_text=log_row.claim_b_text,
            relation=verdict.relation, conflict_type=verdict.conflict_type,
            detail=verdict.detail, suggestion=verdict.suggestion,
            confidence=verdict.confidence,
        )
        events.record(
            self._session, user_id=user_id, event_type=events.L2_JUDGMENT_REVIEWED,
            payload={
                "pair_key": log_row.pair_key,
                "review_state": verdict.review_state,
                "overturned": verdict.overturned,
                "initial_relation": log_row.relation,
                "final_relation": verdict.relation,
            },
        )
        return verdict

    def _run_reviews(
        self,
        *,
        user_id: str,
        result: L2ScanResult,
        xrepo: ConflictRepository,
        log_repo,
        suppressed_types: set[str],
        candidates: list[tuple[float, object, str]],
    ) -> None:
        """二段复核调度：先补上轮欠账（pending 队列），再处理本轮候选。

        预算是硬闸门（l2_review_max_per_scan，按复核调用次数计）：超出的原判行标
        pending，下轮扫描先补——只花复核调用、不重判。本轮候选按「|校准值 −
        决策阈值| 越小越优先」送复核（边界样本的信息量最大）。
        """
        budget = settings.l2_review_max_per_scan

        for row in log_repo.list_pending_review(user_id, limit=budget):
            if result.reviews_run >= budget:
                result.reviews_pending += 1  # 仍欠着，下轮继续补
                continue
            self._review_pair(
                user_id=user_id, log_row=row, log_repo=log_repo, xrepo=xrepo,
                result=result, suppressed_types=suppressed_types,
            )

        for _distance, log_row, initial_suggestion in candidates:
            if result.reviews_run >= budget:
                log_repo.mark_review_state(log_row, "pending")
                self._session.commit()
                result.reviews_pending += 1
                continue
            self._review_pair(
                user_id=user_id, log_row=log_row, log_repo=log_repo, xrepo=xrepo,
                result=result, suppressed_types=suppressed_types,
                initial_suggestion=initial_suggestion,
            )