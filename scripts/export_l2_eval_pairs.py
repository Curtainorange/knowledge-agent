"""从 l2_judgment_logs 分层抽样导出候选对，供人工填 gold 标签。

只读脚本，不写库不调 LLM。每个 relation 各取若干条，优先取
confidence 0.4-0.7 的边界样本（判得犹豫的对最值得标注）。
矛盾类可按弱真值回填一半标签：accepted → 矛盾成立，ignored → 不成立。

用法：python scripts/export_l2_eval_pairs.py [输出路径]
默认输出到 tests/data/l2_judge_eval_candidates.json（不覆盖已有标注集）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.domain.models.conflict import Conflict
from app.domain.models.l2_judgment_log import L2JudgmentLog

PER_RELATION = 6
BORDER = (0.4, 0.7)


def _score(row: L2JudgmentLog) -> float:
    """边界样本优先：confidence 距 0.5 越近越值得标注。"""
    return abs((row.confidence or 0.0) - 0.5)


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tests/data/l2_judge_eval_candidates.json")
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
    session = sessionmaker(bind=engine)()

    rows = list(session.scalars(select(L2JudgmentLog).order_by(L2JudgmentLog.created_at.desc())))
    if not rows:
        raise SystemExit("l2_judgment_logs 为空，先跑一轮 L2 扫描")

    # pair_key → 用户反馈（弱真值回填线索）
    feedback = {
        c.pair_key: c.user_state
        for c in session.scalars(select(Conflict))
    }

    picked: list[L2JudgmentLog] = []
    for relation in ("矛盾", "互补", "断层", "无关"):
        same = [r for r in rows if r.relation == relation and r not in picked]
        border = [r for r in same if BORDER[0] <= (r.confidence or 0) <= BORDER[1]]
        rest = [r for r in same if r not in border]
        border.sort(key=_score)
        picked.extend((border + rest)[:PER_RELATION])

    items = []
    for i, r in enumerate(picked, 1):
        items.append({
            "id": f"C{i:02d}",
            "title_a": r.title_a,
            "title_b": r.title_b,
            "claim_a": r.claim_a_text,
            "claim_b": r.claim_b_text,
            "model_relation": r.relation,
            "model_conflict_type": r.conflict_type,
            "model_confidence": r.confidence,
            "user_feedback": feedback.get(r.pair_key, ""),
            "gold_relation": "",
            "gold_conflict_type": "",
            "note": "",
        })

    out.write_text(
        json.dumps({"version": "v1", "items": items}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"导出 {len(items)} 条候选 → {out}")
    print("人工填 gold_relation（矛盾类补 gold_conflict_type）后，"
          "可将条目并入 tests/data/l2_judge_eval.json")


if __name__ == "__main__":
    main()
