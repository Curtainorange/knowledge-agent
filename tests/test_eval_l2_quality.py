"""评测跑批脚本（scripts/eval_l2_quality.py）的 --golden 双臂逻辑测试。

脚本用 importlib 按路径加载（scripts/ 非包）；网关注入避免真实 LLM——
复核触发/合议的算法本体已在 test_l2_judge_quality.py 全覆盖，这里只验
跑批编排：两臂各自的样本行、复核调用按需发出、指标对比块输出。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.llm.completion import Completion

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_l2_quality.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("eval_l2_quality", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class StubGateway:
    """按 task_type 回放预设文本（与 tests/test_l2.py 的 FakeProvider 同思路）。"""

    def __init__(self, rows_by_task: dict[str, list]) -> None:
        self.rows_by_task = {k: list(v) for k, v in rows_by_task.items()}
        self.calls: list[str] = []

    def chat(self, *, task_type="default", messages=None, **kwargs) -> Completion:
        self.calls.append(task_type)
        rows = self.rows_by_task.get(task_type) or []
        text = rows.pop(0) if rows else json.dumps({"relation": "无关", "confidence": 0.9})
        return Completion(
            text=text, prompt_tokens=1, completion_tokens=1, finish_reason="stop",
            model="stub", reasoning=False,
        )


_CONFLICT_MID = json.dumps({
    "relation": "矛盾", "conflict_type": "立场对立",
    "detail": "整块时间与碎片时间的立场对立", "suggestion": "统一", "confidence": 0.65,
})
_CONFLICT_HIGH = json.dumps({
    "relation": "矛盾", "conflict_type": "立场对立",
    "detail": "整块时间与碎片时间的立场对立", "suggestion": "统一", "confidence": 0.95,
})
_REVIEW_OVERTURN = json.dumps({
    "relation": "无关", "conflict_type": "", "detail": "不在同一层面", "suggestion": "",
    "confidence": 0.9, "uphold_reason": "u", "overturn_reason": "o",
})


def test_golden_with_review_runs_review_and_reports_comparison(capsys):
    """低置信矛盾触发复核：两臂指标 + 对比块都输出。"""
    mod = _load_module()
    gateway = StubGateway({
        "conflict_detection": [_CONFLICT_MID],
        "conflict_review": [_REVIEW_OVERTURN],
    })

    rc = mod.run_golden(1, with_review=True, gateway=gateway)
    out = capsys.readouterr().out

    assert rc == 0
    assert gateway.calls.count("conflict_detection") == 1
    assert gateway.calls.count("conflict_review") == 1, "初判落在复核带才发复核调用"
    assert "复核：矛盾 → 无关" in out
    assert "初判 vs 终判" in out
    assert "终判指标" in out


def test_golden_without_review_single_arm(capsys):
    """不带 --with-review：只跑初判单臂，不发复核调用。"""
    mod = _load_module()
    gateway = StubGateway({"conflict_detection": [_CONFLICT_MID]})

    rc = mod.run_golden(1, with_review=False, gateway=gateway)
    out = capsys.readouterr().out

    assert rc == 0
    assert gateway.calls.count("conflict_review") == 0
    assert "初判指标" in out
    assert "初判 vs 终判" not in out


def test_golden_with_review_skips_review_for_high_confidence(capsys):
    """高置信矛盾（校准 > 0.75）不复核；对比块照出（两臂一致）。"""
    mod = _load_module()
    gateway = StubGateway({"conflict_detection": [_CONFLICT_HIGH]})

    rc = mod.run_golden(1, with_review=True, gateway=gateway)
    out = capsys.readouterr().out

    assert rc == 0
    assert gateway.calls.count("conflict_review") == 0
    assert "初判 vs 终判" in out


def test_golden_review_failure_falls_back_to_initial(capsys):
    """复核输出破损：终判沿用初判，不中断跑批。"""
    mod = _load_module()
    gateway = StubGateway({
        "conflict_detection": [_CONFLICT_MID],
        "conflict_review": ["broken json {"],
    })

    rc = mod.run_golden(1, with_review=True, gateway=gateway)
    out = capsys.readouterr().out

    assert rc == 0
    assert "复核失败沿用初判" in out
    assert "初判 vs 终判" in out
