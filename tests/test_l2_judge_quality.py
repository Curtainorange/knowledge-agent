"""L2 判定质量层单测：置信度校准 / 复核触发 / 复核合议——纯函数，不触模型不触库。"""
from __future__ import annotations

import pytest

from app.agent.l2_judge_quality import (
    FinalVerdict,
    PairSignals,
    calibrate_l2_confidence,
    cites_evidence,
    merge_review,
    needs_review,
)


def _signals(**kwargs) -> PairSignals:
    base = dict(
        relation="矛盾", polarity_opposite=True, cross_source=True, sim=0.6,
        text_a_len=30, text_b_len=30, evidence_cited=True,
    )
    base.update(kwargs)
    return PairSignals(**base)


# ---------- 置信度校准 ----------


def test_calibration_clamps_to_unit_interval():
    assert calibrate_l2_confidence(1.0, _signals()) <= 1.0
    assert calibrate_l2_confidence(0.0, _signals(relation="互补")) >= 0.0


def test_calibration_support_scales_short_text_down():
    long_pair = _signals(text_a_len=40, text_b_len=40)
    short_pair = _signals(text_a_len=5, text_b_len=5)
    assert calibrate_l2_confidence(0.9, short_pair) < calibrate_l2_confidence(0.9, long_pair)


def test_calibration_support_floor_applies():
    # 极短文本打到保底 0.5，不归零（残句的判定打折，但不作废）
    val = calibrate_l2_confidence(1.0, _signals(text_a_len=1, text_b_len=1, evidence_cited=False))
    assert 0.4 < val <= 0.6


def test_calibration_contradiction_signal_directions():
    """矛盾判定：对齐信号抬升、背离信号压低。"""
    aligned = _signals()
    misaligned = _signals(polarity_opposite=False, cross_source=False, sim=0.1)
    assert calibrate_l2_confidence(0.8, aligned) > calibrate_l2_confidence(0.8, misaligned)


def test_calibration_complement_cross_source_bonus():
    base = _signals(relation="互补", polarity_opposite=False, evidence_cited=False, sim=None, cross_source=False)
    cross = _signals(relation="互补", polarity_opposite=False, evidence_cited=False, sim=None, cross_source=True)
    assert calibrate_l2_confidence(0.8, cross) > calibrate_l2_confidence(0.8, base)


def test_calibration_neutral_relation_only_scales():
    """断层/无关不入库，不做信号加成——两种信号集结果相同。"""
    a = _signals(relation="无关", evidence_cited=True, polarity_opposite=True)
    b = _signals(relation="无关", evidence_cited=False, polarity_opposite=False)
    assert calibrate_l2_confidence(0.7, a) == calibrate_l2_confidence(0.7, b)


def test_calibration_monotonic_in_model_confidence():
    sig = _signals()
    assert calibrate_l2_confidence(0.9, sig) >= calibrate_l2_confidence(0.5, sig)


# ---------- 引用证据检测 ----------


def test_cites_evidence_matches_substring():
    detail = "A 说「应当坚守既定战略至少三年」，B 主张每月调整"
    assert cites_evidence(detail, "应当坚守既定战略至少三年不因波动动摇", "其他主张文本内容足够长")
    assert not cites_evidence("空泛的套话", "应当坚守既定战略至少三年不因波动动摇", "其他主张文本内容足够长")


def test_cites_evidence_empty_inputs():
    assert not cites_evidence("", "甲", "乙")
    assert not cites_evidence("有内容", "", "乙")


# ---------- 复核触发 ----------


def test_needs_review_only_contradiction_in_band():
    assert needs_review(relation="矛盾", calibrated=0.5, enabled=True, lo=0.35, hi=0.75)
    assert not needs_review(relation="互补", calibrated=0.5, enabled=True, lo=0.35, hi=0.75)
    assert not needs_review(relation="矛盾", calibrated=0.2, enabled=True, lo=0.35, hi=0.75)
    assert not needs_review(relation="矛盾", calibrated=0.9, enabled=True, lo=0.35, hi=0.75)


def test_needs_review_respects_disabled_switch():
    assert not needs_review(relation="矛盾", calibrated=0.5, enabled=False, lo=0.35, hi=0.75)


def test_needs_review_band_edges_inclusive():
    assert needs_review(relation="矛盾", calibrated=0.35, enabled=True, lo=0.35, hi=0.75)
    assert needs_review(relation="矛盾", calibrated=0.75, enabled=True, lo=0.35, hi=0.75)


# ---------- 复核合议 ----------


def test_merge_review_upheld_boosts_confidence():
    verdict = merge_review(
        "矛盾", "立场对立", "detail", "suggestion", 0.6,
        review_relation="矛盾", review_type="立场对立",
        review_detail="rd", review_suggestion="rs", review_calibrated=0.7,
    )
    assert verdict.review_state == "upheld"
    assert not verdict.overturned
    assert verdict.relation == "矛盾"
    # mean(0.6, 0.7) + 0.05 = 0.7
    assert verdict.confidence == pytest.approx(0.7, abs=1e-3)


def test_merge_review_overturned_takes_review_min_confidence():
    verdict = merge_review(
        "矛盾", "立场对立", "detail", "suggestion", 0.6,
        review_relation="无关", review_type="",
        review_detail="其实无关", review_suggestion="", review_calibrated=0.8,
    )
    assert verdict.review_state == "overturned"
    assert verdict.overturned
    assert verdict.relation == "无关"
    assert verdict.conflict_type == ""   # 非矛盾不留类型
    assert verdict.suggestion == ""
    assert verdict.confidence == pytest.approx(0.6, abs=1e-3)  # min(0.6, 0.8)


def test_merge_review_overturned_to_contradiction_keeps_type():
    verdict = merge_review(
        "矛盾", "立场对立", "d", "s", 0.4,
        review_relation="矛盾", review_type="前提对立",
        review_detail="rd", review_suggestion="rs", review_calibrated=0.9,
    )
    # 同判（都是矛盾）→ upheld，类型取初判
    assert verdict.review_state == "upheld"
    assert verdict.conflict_type == "立场对立"

    verdict = merge_review(
        "互补", "", "d", "", 0.4,
        review_relation="矛盾", review_type="前提对立",
        review_detail="rd", review_suggestion="rs", review_calibrated=0.9,
    )
    assert verdict.overturned
    assert verdict.relation == "矛盾"
    assert verdict.conflict_type == "前提对立"  # 翻案成矛盾要带类型
    assert verdict.confidence == pytest.approx(0.4, abs=1e-3)  # 分歧即低置信


def test_final_verdict_is_dataclass():
    v = FinalVerdict("矛盾", "t", "d", "s", 0.5, "upheld", False)
    assert v.overturned is False
