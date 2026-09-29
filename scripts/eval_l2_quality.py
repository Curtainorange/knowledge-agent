"""L2 判定质量评测跑批。

双模式：
- --from-logs：零 LLM 成本。用 conflicts.user_state 反馈做弱真值，
  输出判定精确率、置信度×accepted 率校准曲线、按 conflict_type 的忽略率。
  随时可跑，是「线上判得准不准」的免费体温计。
- --golden：真实 LLM 跑人工标注集（tests/data/l2_judge_eval.json），
  输出 relation 准确率 / 矛盾 P/R/F1 / ECE / Brier。烧钱，手动跑，
  默认 --limit 10 成本闸门。（复核前后对比 --with-review 在复核机制落地后启用）

用法（项目根目录）：
    python scripts/eval_l2_quality.py --from-logs
    python scripts/eval_l2_quality.py --golden --limit 10
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


def run_golden(limit: int) -> int:
    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    items = data["items"][:limit]
    if not items:
        print("评测集为空")
        return 1

    from app.llm.gateway import ModelGateway
    from app.llm.structure import parse_structured
    from app.agent.l2_orchestrator import ConflictJudgment
    from app.llm.prompts import L2_JUDGE

    gateway = ModelGateway()
    print(f"供应商：{settings.model_provider}  样本数：{len(items)}")
    rows: list[EvalRow] = []
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
        completion = gateway.chat(
            task_type="conflict_detection", messages=messages,
            prompt_version=L2_JUDGE.version, json_model=ConflictJudgment,
        )
        try:
            judgment = parse_structured(
                completion.text, validator=lambda d: ConflictJudgment(**d)
            )
            predicted, confidence = judgment.relation, judgment.confidence
        except Exception as exc:  # 解析失败按「无关」计并标注，不中断跑批
            print(f"  [{it['id']}] 解析失败：{exc}")
            predicted, confidence = "无关", 0.0
        rows.append(EvalRow(predicted=predicted, gold=it["gold_relation"], confidence=confidence))
        print(f"  [{it['id']}] 预测={predicted}({confidence})  真值={it['gold_relation']}")

    print(f"\n== 标注集指标（limit={limit}）==")
    print(f"relation 准确率: {relation_accuracy(rows)}")
    print(f"矛盾二分类: {binary_conflict_metrics(rows)}")
    print(f"ECE: {expected_calibration_error(rows)}   Brier: {brier_conflict(rows)}")
    for b in calibration_buckets(rows):
        if b["n"]:
            print(f"  桶 [{b['lo']}, {b['hi']}]  n={b['n']}  conf={b['mean_conf']}  acc={b['accuracy']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="L2 判定质量评测")
    parser.add_argument("--from-logs", action="store_true", help="弱真值指标（零 LLM 成本）")
    parser.add_argument("--golden", action="store_true", help="跑人工标注集（烧 LLM，手动跑）")
    parser.add_argument("--limit", type=int, default=10, help="--golden 样本上限（成本闸门）")
    args = parser.parse_args()

    if not args.from_logs and not args.golden:
        parser.print_help()
        return 0

    if args.golden:
        return run_golden(args.limit)

    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()
    return run_from_logs(session)


if __name__ == "__main__":
    raise SystemExit(main())
