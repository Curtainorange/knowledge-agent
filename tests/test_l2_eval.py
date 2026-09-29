"""L2 评测指标单测：构造已知答案数据验算，不触模型不触库。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agent.l2_eval import (
    CONFLICT,
    RELATIONS,
    EvalRow,
    binary_conflict_metrics,
    brier_conflict,
    calibration_buckets,
    confusion_matrix,
    expected_calibration_error,
    relation_accuracy,
    weak_label_metrics,
)


def _rows(specs: list[tuple[str, str, float]]) -> list[EvalRow]:
    return [EvalRow(predicted=p, gold=g, confidence=c) for p, g, c in specs]


# ---------- relation 准确率 / 混淆矩阵 ----------


def test_relation_accuracy_known_answer():
    rows = _rows([
        ("矛盾", "矛盾", 0.9),
        ("矛盾", "互补", 0.8),
        ("无关", "无关", 0.7),
        ("断层", "无关", 0.6),
    ])
    assert relation_accuracy(rows) == 0.5  # 4 中 2


def test_relation_accuracy_empty_is_zero():
    assert relation_accuracy([]) == 0.0


def test_confusion_matrix_counts_cells():
    rows = _rows([
        ("矛盾", "矛盾", 0.9),
        ("矛盾", "互补", 0.8),
        ("矛盾", "互补", 0.7),
    ])
    matrix = confusion_matrix(rows)
    assert matrix[("矛盾", "矛盾")] == 1
    assert matrix[("互补", "矛盾")] == 2


# ---------- 矛盾二分类 P/R/F1 ----------


def test_binary_conflict_metrics_known_answer():
    rows = _rows([
        ("矛盾", "矛盾", 0.9),    # tp
        ("矛盾", "矛盾", 0.8),    # tp
        ("矛盾", "互补", 0.7),    # fn（漏判）
        ("互补", "矛盾", 0.6),    # fp（误判）
        ("无关", "无关", 0.5),    # tn
    ])
    m = binary_conflict_metrics(rows)
    assert m["tp"] == 2 and m["fp"] == 1 and m["fn"] == 1
    assert m["precision"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["recall"] == pytest.approx(2 / 3, abs=1e-3)
    assert m["f1"] == pytest.approx(2 / 3, abs=1e-3)


def test_binary_conflict_metrics_no_predictions_is_zero():
    m = binary_conflict_metrics(_rows([("互补", "矛盾", 0.9)]))
    assert m["precision"] == 0.0 and m["recall"] == 0.0 and m["f1"] == 0.0


# ---------- 置信度校准 ----------


def test_calibration_buckets_partition_samples():
    rows = _rows([
        ("矛盾", "矛盾", 0.1),
        ("矛盾", "互补", 0.55),
        ("矛盾", "矛盾", 0.9),
    ])
    buckets = calibration_buckets(rows, width=0.2)
    assert sum(b["n"] for b in buckets) == 3
    low = next(b for b in buckets if b["lo"] == 0.0)
    assert low["n"] == 1 and low["mean_conf"] == 0.1


def test_ece_zero_when_perfectly_calibrated():
    # 置信度 0.8 的样本 80% 对：构造 8 对 2 错
    rows = _rows([("矛盾", "矛盾", 0.8)] * 8 + [("矛盾", "互补", 0.8)] * 2)
    # 同一桶内 mean_conf=0.8，accuracy=0.8 → ECE=0
    assert expected_calibration_error(rows, width=0.2) == 0.0


def test_ece_high_when_confidence_misaligned():
    # 全部自信 0.95 但全错 → ECE ≈ 0.95
    rows = _rows([("矛盾", "互补", 0.95)] * 5)
    assert expected_calibration_error(rows, width=0.2) == pytest.approx(0.95, abs=1e-3)


def test_ece_empty_is_zero():
    assert expected_calibration_error([]) == 0.0


def test_calibration_buckets_rejects_bad_width():
    with pytest.raises(ValueError):
        calibration_buckets([], width=0)


def test_brier_conflict_known_answer():
    # 满置信预测矛盾：一错（gold 互补）误差 1.0，一对（gold 矛盾）误差 0 → 均值 0.5
    rows = _rows([
        ("矛盾", "互补", 1.0),
        ("矛盾", "矛盾", 1.0),
    ])
    assert brier_conflict(rows) == pytest.approx(0.5, abs=1e-4)


# ---------- 弱真值指标 ----------


def test_weak_label_precision_and_insufficient_note():
    feedback = [("accepted", 0.9)] * 6 + [("ignored", 0.8)] * 4
    m = weak_label_metrics(feedback)
    assert m["precision"] == 0.6
    assert m["note"] == ""


def test_weak_label_insufficient_sample_returns_minus_one():
    m = weak_label_metrics([("accepted", 0.9), ("ignored", 0.8)])
    assert m["precision"] == -1.0
    assert "样本不足" in m["note"]


def test_weak_label_calibration_buckets_count_accepted_rate():
    feedback = [("accepted", 0.9), ("accepted", 0.8), ("ignored", 0.4), ("unseen", 0.3)]
    m = weak_label_metrics(feedback)
    high = next(b for b in m["calibration_by_bucket"] if b["lo"] == 0.75)
    low = next(b for b in m["calibration_by_bucket"] if b["lo"] == 0.0)
    assert high["accepted_rate"] == 1.0
    assert low["accepted_rate"] == 0.0   # ignored 计入分母
    assert low["n"] == 2                  # ignored + unseen 都在 0~0.5 桶


# ---------- 评测集 JSON 结构校验 ----------


def test_eval_dataset_schema():
    path = Path(__file__).parent / "data" / "l2_judge_eval.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == "v1"
    items = data["items"]
    assert len(items) >= 5
    ids = [it["id"] for it in items]
    assert len(ids) == len(set(ids)), "评测集 id 必须唯一"
    for it in items:
        for field in ("title_a", "title_b", "claim_a", "claim_b", "gold_relation", "note"):
            assert it.get(field), f"{it['id']} 缺字段 {field}"
        assert it["gold_relation"] in RELATIONS
        if it["gold_relation"] == CONFLICT:
            assert it.get("gold_conflict_type"), f"{it['id']} 矛盾类必须填 gold_conflict_type"


def test_eval_dataset_has_review_band_conflicts():
    """标注集须含中置信边界样本（band=review 的矛盾对），复核翻案/救回才有证据基础。"""
    path = Path(__file__).parent / "data" / "l2_judge_eval.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    band_items = [it for it in data["items"] if it.get("band") == "review"]
    assert len(band_items) >= 3, "复核带边界样本至少 3 条"
    for it in band_items:
        assert it["gold_relation"] == CONFLICT, f"{it['id']} 边界样本应为矛盾类"
        assert it.get("gold_conflict_type"), f"{it['id']} 矛盾类必须填 gold_conflict_type"
