"""L2 判定质量评测跑批。

双模式：
- --from-logs：零 LLM 成本。用 conflicts.user_state 反馈做弱真值，
  输出判定精确率、置信度×accepted 率校准曲线、按 conflict_type 的忽略率。
  随时可跑，是「线上判得准不准」的免费体温计。
- --golden：真实 LLM 跑人工标注集（tests/data/l2_judge_eval.json），
  输出 relation 准确率 / 矛盾 P/R/F1 / ECE / Brier。烧钱，手动跑，
  默认 --limit 10 成本闸门。--with-review 时对低置信矛盾加跑复核合议，
  同批样本输出「初判 vs 终判」两套指标直接对比——复核有没有用的证据。
  置信度口径与运行时一致：用校准值（阈值判的也是校准值），不是模型裸自报。
  解析失败重试 1 次（对齐运行时「不写判定、下轮重判」），仍失败剔出指标并
  单独报告，另给「失败按错计」的悲观下界对照——不把管道故障冒充成判定错误。

用法（项目根目录）：
    python scripts/eval_l2_quality.py --from-logs
    python scripts/eval_l2_quality.py --golden --limit 10
    python scripts/eval_l2_quality.py --golden --with-review --limit 10
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.agent.l2_eval import (
    EvalRow,
    binary_conflict_metrics,
    brier_conflict,
    calibration_buckets,
    expected_calibration_error,
    relation_accuracy,
    weak_label_metrics,
)
from app.core.config import settings
from app.domain.models.conflict import Conflict
from app.domain.models.l2_judgment_log import L2JudgmentLog

EVAL_PATH = Path("tests/data/l2_judge_eval.json")


def run_from_logs(session) -> int:
    conflicts = list(session.scalars(select(Conflict)))
    logs = list(session.scalars(select(L2JudgmentLog)))
    conf_by_pair = {c.pair_key: c for c in conflicts}

    feedback = [(c.user_state, c.confidence or 0.0) for c in conflicts]
    metrics = weak_label_metrics(feedback)

    print("== 弱真值指标（conflicts.user_state 反馈）==")
    if metrics["note"]:
        print(f"precision: {metrics['note']}")
    else:
        print(f"precision (accepted/(accepted+ignored)): {metrics['precision']}")
    print(f"accepted={metrics['n_accepted']}  ignored={metrics['n_ignored']}")
    print("置信度桶 × accepted 率（校准曲线）：")
    for b in metrics["calibration_by_bucket"]:
        rate = "样本不足" if b["accepted_rate"] < 0 else b["accepted_rate"]
        print(f"  [{b['lo']}, {b['hi']}]  n={b['n']}  accepted_rate={rate}")

    # 高置信桶 vs 低置信桶的 accepted 率差（验收：≥ 20pp，样本≥10 时才有意义）
    buckets = metrics["calibration_by_bucket"]
    low, high = buckets[0], buckets[-1]
    if low["accepted_rate"] >= 0 and high["accepted_rate"] >= 0 and (low["n"] + high["n"]) >= 10:
        gap = round(high["accepted_rate"] - low["accepted_rate"], 4)
        print(f"高置信(≥0.75) − 低置信(<0.5) accepted 率差: {gap}")

    # 按 conflict_type 的忽略率：找误判集中地（改进提示词/抑制的靶子）
    type_states: dict[str, Counter] = {}
    for c in conflicts:
        if not c.conflict_type:
            continue
        type_states.setdefault(c.conflict_type, Counter())[c.user_state] += 1
    if type_states:
        print("按 conflict_type 的反馈分布（ignored 集中处 = 误判靶子）：")
        for t, counter in sorted(type_states.items()):
            print(f"  {t}: {dict(counter)}")

    print(f"\n判定日志共 {len(logs)} 条（pair_key 去重后 "
          f"{len({r.pair_key for r in logs})} 对）；冲突 {len(conflicts)} 条")
    return 0


def _claim_sim(claim_a: str, claim_b: str) -> float | None:
    """送判对的余弦（与运行时语义通道同口径）；embedding 不可用时 None。"""
    try:
        from app.retrieval.embedding import build_embedding
        from app.retrieval.vector_store import cos_sim

        embed = build_embedding()
        va, vb = list(embed.embed([claim_a, claim_b]))
        return cos_sim(va, vb)
    except Exception:  # noqa: BLE001 - sim 只是校准信号之一，缺了不影响跑批
        return None


def run_golden(limit: int, *, with_review: bool = False, gateway=None) -> int:
    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    items = data["items"][:limit]
    if not items:
        print("评测集为空")
        return 1

    from app.agent.l2_judge_quality import (
        PairSignals,
        calibrate_l2_confidence,
        cites_evidence,
        merge_review,
        needs_review,
    )
    from app.agent.l2_orchestrator import ConflictJudgment, ReviewJudgment
    from app.llm.gateway import ModelGateway
    from app.llm.prompts import L2_JUDGE, L2_REVIEW
    from app.llm.structure import parse_structured

    if gateway is None:
        gateway = ModelGateway()
        print(f"供应商：{settings.model_provider}  样本数：{len(items)}")
    else:
        print(f"供应商：注入网关  样本数：{len(items)}")

    def _signals(relation: str, detail: str, sim: float | None, it: dict) -> PairSignals:
        # 评测集无极性/来源标注：极性相反与跨来源信号取 False（两臂口径一致即可比）
        return PairSignals(
            relation=relation,
            polarity_opposite=False,
            cross_source=False,
            sim=sim,
            text_a_len=len(it["claim_a"]),
            text_b_len=len(it["claim_b"]),
            evidence_cited=cites_evidence(detail, it["claim_a"], it["claim_b"]),
        )

    rows_initial: list[EvalRow] = []
    rows_final: list[EvalRow] = []
    parse_failed: list[str] = []  # 重试后仍解析失败的样本 id（剔出指标，单独报告）
    reviews_run = 0
    for it in items:
        # 与 L2 判定完全同口径（_judge_pair）：同提示词、同消息格式、同解析路径
        messages = [
            {"role": "system", "content": L2_JUDGE.text},
            {
                "role": "user",
                "content": (
                    f"主张A（来自《{it['title_a']}》）：{it['claim_a']}\n"
                    f"主张B（来自《{it['title_b']}》）：{it['claim_b']}\n\n"
                    "请判定两条主张的关系并输出 JSON。"
                ),
            },
        ]
        sim = _claim_sim(it["claim_a"], it["claim_b"])
        judgment = None
        predicted, confidence = "无关", 0.0
        # 解析失败重试 1 次：运行时「解析失败不写判定」，下轮扫描会重判该对，
        # 评测重试一次是对齐该语义。仍失败则剔出指标——运行时它不是一次「判无关」，
        # 按无关计分是把管道故障冒充成判定错误，会虚低准确率。
        for attempt in (1, 2):
            try:
                completion = gateway.chat(
                    task_type="conflict_detection", messages=messages,
                    prompt_version=L2_JUDGE.version, json_model=ConflictJudgment,
                )
                judgment = parse_structured(
                    completion.text, validator=lambda d: ConflictJudgment(**d)
                )
                calibrated = calibrate_l2_confidence(
                    judgment.confidence, _signals(judgment.relation, judgment.detail, sim, it)
                )
                predicted, confidence = judgment.relation, calibrated
                break
            except Exception as exc:  # noqa: BLE001 - 单条解析失败不中断跑批
                print(f"  [{it['id']}] 解析失败（第 {attempt} 次）：{exc}")
        if judgment is None:
            parse_failed.append(it["id"])
            continue
        rows_initial.append(EvalRow(predicted=predicted, gold=it["gold_relation"], confidence=confidence))

        verdict_relation, verdict_conf = predicted, confidence
        if (
            with_review
            and judgment is not None
            and needs_review(
                relation=judgment.relation, calibrated=confidence,
                enabled=True, lo=settings.l2_review_lo, hi=settings.l2_review_hi,
            )
        ):
            # 复核与 _review_pair 完全同口径；合议规则 merge_review（模型不算术）
            review_messages = [
                {"role": "system", "content": L2_REVIEW.text},
                {
                    "role": "user",
                    "content": (
                        f"主张A（来自《{it['title_a']}》）：{it['claim_a']}\n"
                        f"主张B（来自《{it['title_b']}》）：{it['claim_b']}\n\n"
                        f"初判：relation={judgment.relation}，conflict_type={judgment.conflict_type}，"
                        f"detail={judgment.detail}\n"
                        "请换角度重新审查并输出 JSON。"
                    ),
                },
            ]
            try:
                completion = gateway.chat(
                    task_type="conflict_review", messages=review_messages,
                    prompt_version=L2_REVIEW.version, json_model=ReviewJudgment,
                )
                review = parse_structured(
                    completion.text, validator=lambda d: ReviewJudgment(**d)
                )
                review_calibrated = calibrate_l2_confidence(
                    review.confidence, _signals(review.relation, review.detail, sim, it)
                )
                verdict = merge_review(
                    judgment.relation, judgment.conflict_type, judgment.detail,
                    judgment.suggestion, confidence,
                    review_relation=review.relation, review_type=review.conflict_type,
                    review_detail=review.detail, review_suggestion=review.suggestion,
                    review_calibrated=review_calibrated,
                )
                verdict_relation, verdict_conf = verdict.relation, verdict.confidence
                reviews_run += 1
                print(f"  [{it['id']}] 复核：{judgment.relation} → {verdict.relation}"
                      f"（{verdict.review_state}，合议 {verdict.confidence}）")
            except Exception as exc:  # 复核失败：终判沿用初判，不中断跑批
                print(f"  [{it['id']}] 复核失败沿用初判：{exc}")
        rows_final.append(EvalRow(predicted=verdict_relation, gold=it["gold_relation"], confidence=verdict_conf))
        print(f"  [{it['id']}] 初判={predicted}({confidence})  真值={it['gold_relation']}")

    def _report(label: str, rows: list[EvalRow]) -> None:
        print(f"\n== {label}（n={len(rows)}）==")
        print(f"relation 准确率: {relation_accuracy(rows)}")
        print(f"矛盾二分类: {binary_conflict_metrics(rows)}")
        print(f"ECE: {expected_calibration_error(rows)}   Brier: {brier_conflict(rows)}")
        for b in calibration_buckets(rows):
            if b["n"]:
                print(f"  桶 [{b['lo']}, {b['hi']}]  n={b['n']}  conf={b['mean_conf']}  acc={b['accuracy']}")

    if parse_failed:
        n_total, n_kept = len(items), len(rows_initial)
        n_correct = sum(1 for r in rows_initial if r.predicted == r.gold)
        print(f"\n== 解析失败样本（{len(parse_failed)} 条：{','.join(parse_failed)}）==")
        print("已剔出指标（运行时语义：解析失败不写判定，下轮扫描重判，不是一次误判）。")
        print(f"对照口径——失败按错计（悲观下界）: {round(n_correct / n_total, 4)}"
              f"（{n_correct}/{n_total}）；有效样本准确率: {relation_accuracy(rows_initial)}"
              f"（{n_correct}/{n_kept}）")

    _report(f"初判指标（limit={limit}）", rows_initial)
    if with_review:
        _report(f"终判指标（含复核合议，复核 {reviews_run} 对）", rows_final)
        acc_a, acc_b = relation_accuracy(rows_initial), relation_accuracy(rows_final)
        ece_a = expected_calibration_error(rows_initial)
        ece_b = expected_calibration_error(rows_final)
        print("\n== 初判 vs 终判 ==")
        print(f"relation 准确率: {acc_a} → {acc_b}")
        print(f"ECE: {ece_a} → {ece_b}（降为优）")
        f1_a = binary_conflict_metrics(rows_initial).get("f1")
        f1_b = binary_conflict_metrics(rows_final).get("f1")
        print(f"矛盾 F1: {f1_a} → {f1_b}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="L2 判定质量评测")
    parser.add_argument("--from-logs", action="store_true", help="弱真值指标（零 LLM 成本）")
    parser.add_argument("--golden", action="store_true", help="跑人工标注集（烧 LLM，手动跑）")
    parser.add_argument("--limit", type=int, default=10, help="--golden 样本上限（成本闸门）")
    parser.add_argument("--with-review", action="store_true",
                        help="--golden 时对低置信矛盾加跑复核合议，输出初判 vs 终判对比")
    args = parser.parse_args()

    if not args.from_logs and not args.golden:
        parser.print_help()
        return 0

    if args.golden:
        return run_golden(args.limit, with_review=args.with_review)

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()
    return run_from_logs(session)


if __name__ == "__main__":
    raise SystemExit(main())
