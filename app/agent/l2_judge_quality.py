"""L2 判定质量层：置信度校准、复核触发、复核合议——纯函数，零 I/O 零 LLM。

与 L5 的 calibrate_confidence（ADR-15）同一范式：不信任模型裸自报的
confidence，用本地信号做「证据充分度缩放 + 信号一致性微调」。「模型不算术、
规则做统计」——这里的每一条加减分都是可单测的本地规则。
"""
from __future__ import annotations

from dataclasses import dataclass

CONFLICT = "矛盾"

# 证据充分度：双方主张文本都够长才算证据充分（残句/影子主张自然打折）。
# 标定按中文主张的一句话长度：约 20 字即证据完整，更短按比例打折，保底 0.5。
# 不能按 40 字标定——真实主张多在 15~25 字，打折过狠会让高置信矛盾全被阈值吃掉。
_SUPPORT_LEN = 20.0
_SUPPORT_MIN = 0.5

# 信号一致性微调的封顶（与 L5 的 +0.1 上限同源）
_BONUS_MAX = 0.1


@dataclass(frozen=True)
class PairSignals:
    """送判对的本地信号快照（全部可离线计算，不问模型）。"""

    relation: str
    polarity_opposite: bool = False
    cross_source: bool = False
    sim: float | None = None          # 送判时余弦；topic 通道可能没有
    text_a_len: int = 0
    text_b_len: int = 0
    evidence_cited: bool = False      # detail 是否引用了双方文本片段（本地子串匹配）


def cites_evidence(detail: str, text_a: str, text_b: str, *, min_len: int = 8) -> bool:
    """detail 是否引用了双方主张的文本片段（非模型自评，纯子串匹配）。

    判定说明引用了原文 = 判定有据可查；空话套话拿不到这个加分。
    """
    if not detail or not text_a or not text_b:
        return False
    return any(seg in detail for seg in _fragments(text_a, min_len) + _fragments(text_b, min_len))


def _fragments(text: str, min_len: int) -> list[str]:
    """取文本中长度 ≥ min_len 的连续中文/词片段（粗粒度即可，够做子串匹配）。"""
    t = text.strip()
    if len(t) <= min_len:
        return [t] if t else []
    step = max(1, min_len // 2)
    return [t[i:i + min_len + 4] for i in range(0, len(t) - min_len + 1, step)]


def calibrate_l2_confidence(model_confidence: float, signals: PairSignals) -> float:
    """置信度校准（ADR-15 范式搬到 L2）：支持度缩放 + 信号一致性微调。

    - 证据充分度：min(双方文本长度)/20，clamp [0.5, 1.0]——两条主张都够长
      才算证据充分；影子主张、残句会自然打折。
    - 信号一致性：只用与该 relation 相关的信号加减分，封顶 ±0.1。
      矛盾看极性/跨来源/sim 区间/引用证据；互补看跨来源/引用证据；
      断层/无关不入库，微调无收益，只做缩放。
    """
    conf = float(model_confidence)
    support = min(1.0, max(_SUPPORT_MIN, min(signals.text_a_len, signals.text_b_len) / _SUPPORT_LEN))
    bonus = _consistency_bonus(signals)
    raw = conf * support + bonus
    return round(min(1.0, max(0.0, raw)), 3)


def _consistency_bonus(signals: PairSignals) -> float:
    bonus = 0.0
    if signals.relation == CONFLICT:
        if signals.polarity_opposite:
            bonus += 0.04
        else:
            # 极性相同还判矛盾：要么是深层对立（少见），要么判得勉强
            bonus -= 0.04
        if signals.cross_source:
            bonus += 0.03
        if signals.sim is not None and 0.45 <= signals.sim <= 0.85:
            bonus += 0.03
        if signals.sim is not None and signals.sim < 0.35:
            # topic 硬拉到一起的对：词面差异大，「矛盾」容易是幻觉
            bonus -= 0.05
        if signals.evidence_cited:
            bonus += 0.04
    elif signals.relation == "互补":
        if signals.cross_source:
            bonus += 0.05
        if signals.evidence_cited:
            bonus += 0.03
    return max(-_BONUS_MAX, min(_BONUS_MAX, bonus))


def needs_review(*, relation: str, calibrated: float, enabled: bool,
                 lo: float, hi: float) -> bool:
    """复核触发策略（全是规则）：只复核「关系到入库」的矛盾判定。

    区间 [lo, hi] 横跨丢弃阈值（0.5）：下沿是被丢弃判定的翻案候选，
    上沿是勉强入库判定的把关对象。高置信不复核——花钱买不到信息。
    非矛盾误判（漏判）由评测集度量，不花运行时成本。
    """
    if not enabled:
        return False
    if relation != CONFLICT:
        return False
    return lo <= calibrated <= hi


@dataclass
class FinalVerdict:
    """复核合议结果（模型不算术：同判/分歧的置信度都是规则定的）。"""

    relation: str
    conflict_type: str
    detail: str
    suggestion: str
    confidence: float
    review_state: str        # upheld | overturned
    overturned: bool


def merge_review(
    initial_relation: str,
    initial_type: str,
    initial_detail: str,
    initial_suggestion: str,
    initial_calibrated: float,
    *,
    review_relation: str,
    review_type: str,
    review_detail: str,
    review_suggestion: str,
    review_calibrated: float,
) -> FinalVerdict:
    """合议：同判维持（置信度上调整）/ 不同判推翻（分歧即低置信）。

    - 同判维持：终判置信度 = mean(两校准值) + 0.05（一致性奖励），clamp。
    - 不同判推翻：终判取复核结论，置信度 = min(两校准值)——两次判断
      都没底的结论，置信度自然过不了入库阈值，不会被硬塞给用户。
    """
    same = review_relation == initial_relation
    if same:
        conf = (initial_calibrated + review_calibrated) / 2 + 0.05
        return FinalVerdict(
            relation=initial_relation,
            conflict_type=initial_type or review_type,
            detail=initial_detail or review_detail,
            suggestion=initial_suggestion or review_suggestion,
            confidence=round(min(1.0, max(0.0, conf)), 3),
            review_state="upheld",
            overturned=False,
        )
    conf = min(initial_calibrated, review_calibrated)
    return FinalVerdict(
        relation=review_relation,
        conflict_type=review_type if review_relation == CONFLICT else "",
        detail=review_detail or initial_detail,
        suggestion=review_suggestion if review_relation == CONFLICT else "",
        confidence=round(conf, 3),
        review_state="overturned",
        overturned=True,
    )
