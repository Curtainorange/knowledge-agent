"""L2 判定质量评测指标：纯规则统计，零 I/O 零 LLM。

「模型不算术、规则做统计」——准确率、混淆矩阵、校准误差都是本地算术，
不送模型。两套数据源共用同一批指标函数：

- 人工标注集（tests/data/l2_judge_eval.json）：gold_relation 是真值，
  算 relation 准确率 / 矛盾二分类 P/R/F1 / 置信度校准（ECE、Brier）。
- 弱真值（conflicts.user_state 反馈）：accepted 视作「判对了矛盾」、
  ignored 视作「误判」，样本不足时明说，不给假数字。
"""
from __future__ import annotations

from dataclasses import dataclass

CONFLICT = "矛盾"
RELATIONS = ("矛盾", "互补", "断层", "无关")


@dataclass(frozen=True)
class EvalRow:
    """一次判定的评测样本：预测关系、真值、置信度（校准值优先）。"""

    predicted: str
    gold: str
    confidence: float


def relation_accuracy(rows: list[EvalRow]) -> float:
    """4 类关系准确率。空集返回 0.0（调用方负责报「样本不足」）。"""
    if not rows:
        return 0.0
    correct = sum(1 for r in rows if r.predicted == r.gold)
    return round(correct / len(rows), 4)


def confusion_matrix(rows: list[EvalRow]) -> dict[tuple[str, str], int]:
    """(gold, predicted) -> 计数。只统计出现过的格子。"""
    matrix: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r.gold, r.predicted)
        matrix[key] = matrix.get(key, 0) + 1
    return matrix


def binary_conflict_metrics(rows: list[EvalRow]) -> dict[str, float]:
    """矛盾 vs 非矛盾二分类 P/R/F1——业务主指标（抓没抓出真矛盾）。"""
    tp = sum(1 for r in rows if r.gold == CONFLICT and r.predicted == CONFLICT)
    fp = sum(1 for r in rows if r.gold != CONFLICT and r.predicted == CONFLICT)
    fn = sum(1 for r in rows if r.gold == CONFLICT and r.predicted != CONFLICT)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": float(tp), "fp": float(fp), "fn": float(fn),
    }


def calibration_buckets(rows: list[EvalRow], width: float = 0.2) -> list[dict]:
    """置信度分桶：每桶 {lo, hi, n, mean_conf, accuracy}。

    校准良好的判定：桶内 mean_conf ≈ accuracy。
    """
    if width <= 0:
        raise ValueError("width must be positive")
    n_buckets = int(round(1.0 / width))
    buckets: list[dict] = []
    for i in range(n_buckets):
        lo = round(i * width, 4)
        hi = round(min(1.0, lo + width), 4)
        members = [r for r in rows if lo <= r.confidence < hi or (i == n_buckets - 1 and r.confidence == 1.0)]
        if not members:
            buckets.append({"lo": lo, "hi": hi, "n": 0, "mean_conf": 0.0, "accuracy": 0.0})
            continue
        mean_conf = sum(r.confidence for r in members) / len(members)
        accuracy = sum(1 for r in members if r.predicted == r.gold) / len(members)
        buckets.append({
            "lo": lo, "hi": hi, "n": len(members),
            "mean_conf": round(mean_conf, 4), "accuracy": round(accuracy, 4),
        })
    return buckets


def expected_calibration_error(rows: list[EvalRow], width: float = 0.2) -> float:
    """ECE：各桶 |准确率 − 平均置信度| 按样本量加权。越小越可信。"""
    if not rows:
        return 0.0
    total = len(rows)
    ece = 0.0
    for b in calibration_buckets(rows, width):
        if b["n"] == 0:
            continue
        ece += (b["n"] / total) * abs(b["accuracy"] - b["mean_conf"])
    return round(ece, 4)


def brier_conflict(rows: list[EvalRow]) -> float:
    """矛盾二分类 Brier：把「矛盾」当正类，confidence 为预测为矛盾的概率。

    非矛盾预测的样本按 (1 - confidence) 计入负类误差——只衡量
    「是不是矛盾」这一件事的校准质量。
    """
    if not rows:
        return 0.0
    errors = []
    for r in rows:
        p_conflict = r.confidence if r.predicted == CONFLICT else 1.0 - r.confidence
        y = 1.0 if r.gold == CONFLICT else 0.0
        errors.append((p_conflict - y) ** 2)
    return round(sum(errors) / len(errors), 4)


def weak_label_metrics(feedback: list[tuple[str, float]]) -> dict:
    """弱真值指标：用户反馈（user_state, confidence）→ 判定质量的免费度量。

    - precision：accepted / (accepted + ignored)，矛盾判定在用户眼里的精确率
    - calibration_by_bucket：置信度桶 × accepted 率（线上校准曲线）
    - ignored_rate_by_conflict_type 由调用方另算（这里只收扁平反馈）；
      样本 < 10 时 precision 记 -1.0 并出 note——宁缺毋假。
    """
    accepted = [(s, c) for s, c in feedback if s == "accepted"]
    ignored = [(s, c) for s, c in feedback if s == "ignored"]
    decided = len(accepted) + len(ignored)
    note = ""
    if decided < 10:
        precision = -1.0
        note = f"样本不足（accepted+ignored={decided} < 10），不给精确率"
    else:
        precision = round(len(accepted) / decided, 4)

    buckets = []
    for lo, hi in ((0.0, 0.5), (0.5, 0.75), (0.75, 1.01)):
        members = [(s, c) for s, c in feedback if lo <= c < hi]
        decided_in = [s for s, _ in members if s in ("accepted", "ignored")]
        acc_rate = (
            round(sum(1 for s in decided_in if s == "accepted") / len(decided_in), 4)
            if decided_in else -1.0
        )
        buckets.append({
            "lo": lo, "hi": min(hi, 1.0), "n": len(members),
            "accepted_rate": acc_rate,
        })

    return {
        "precision": precision,
        "n_accepted": len(accepted),
        "n_ignored": len(ignored),
        "calibration_by_bucket": buckets,
        "note": note,
    }
