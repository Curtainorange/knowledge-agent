"""Golden set 回归评测（E 组 / 风险 R6）。

两层防线，都**不触网**（无网 CI 也能拦住回归）：

1. **提示词指纹防脱节**：`tests/golden/prompts.json` 记录了每个提示词的版本号与
   sha256。任何人改了 `app/llm/prompts.py` 里的提示词文本，却没有同步 bump 版本、
   更新指纹，这里就会红——防止「改了提示词却无从追溯、golden 与代码漂移」。

2. **真实产出回放**：`tests/golden/outputs/*.json` 录制了模型真实产出（含曾经踩过
   的坑：字符串值里裸换行、markdown 围栏、前后夹带解释文字）。用各编排器同一套
   `parse_structured` + pydantic validator 回放，断言仍能解析且关键字段齐全——
   防「改了解析器却把旧产出形态改坏」。

为什么必须有它：L3 那次「字符串值里直接换行」让一次调用白花，单元测试与本地 Fake
都拦不住——只有把真实产出录成夹具回放，才能在无网环境里抓住这类回归。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.agent.l1_orchestrator import L1Route
from app.agent.l2_orchestrator import ConflictJudgment, ExtractionResult, ReviewJudgment
from app.agent.l3_orchestrator import BriefDraft, TopicAnalysisResult
from app.agent.l4_orchestrator import DeviationAnalysis, GeneratedPlan
from app.agent.l5_orchestrator import DiagnosisDraft
from app.agent.book_orchestrator import BookRecommendList, ChunkDigest, SummaryDigest
from app.agent.router import KNOWN_CAPABILITIES, CapabilityRoute
from app.llm.prompts import ALL_PROMPTS, prompt_by_name
from app.llm.structure import parse_structured

GOLDEN_DIR = Path(__file__).parent / "golden"
OUTPUTS_DIR = GOLDEN_DIR / "outputs"


def _load_samples(name: str) -> list[dict]:
    path = OUTPUTS_DIR / f"{name}.json"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)


# ---- 第一层：提示词指纹防脱节 -----------------------------------------------


def test_prompt_fingerprints_match_golden():
    """每个提示词的版本号与文本 sha256 必须与 golden 记录一致。

    改文本 → sha256 变 → 这里红 → 强制 bump version 并更新 prompts.json。
    这是「改了提示词却忘了版本化」的硬闸门。
    """
    manifest_path = GOLDEN_DIR / "prompts.json"
    with manifest_path.open(encoding="utf-8") as f:
        manifest = json.load(f)

    names = {p.name for p in ALL_PROMPTS}
    assert set(manifest) == names, (
        f"prompts.json 与 ALL_PROMPTS 不一致：多 {set(manifest) - names}，缺 {names - set(manifest)}"
    )

    for p in ALL_PROMPTS:
        entry = manifest[p.name]
        digest = hashlib.sha256(p.text.encode("utf-8")).hexdigest()
        assert entry["version"] == p.version, (
            f"[{p.name}] 版本号不一致：golden={entry['version']}，代码={p.version}"
        )
        assert entry["sha256"] == digest, (
            f"[{p.name}] 提示词文本被改动但指纹未更新。"
            f"若这是有意为之：把 version 从 {p.version} bump 到下一版，"
            f"并用 scripts 重新生成 prompts.json 的 sha256。"
        )


# ---- 第二层：真实产出回放 ---------------------------------------------------


def _validator_for(name: str):
    return {
        "agent_route": lambda d: CapabilityRoute(**d),
        "l1_route": lambda d: L1Route(**d),
        "l2_extract": lambda d: ExtractionResult(**d),
        "l2_judge": lambda d: ConflictJudgment(**d),
        "l2_review": lambda d: ReviewJudgment(**d),
        "l3_analyze": lambda d: TopicAnalysisResult(**d),
        "l3_brief": lambda d: BriefDraft(**d),
        "l3_item": lambda d: BriefDraft(**d),
        "l4_plan": lambda d: GeneratedPlan(**d),
        "l4_deviate": lambda d: DeviationAnalysis(**d),
        "l5_diagnose": lambda d: DiagnosisDraft(**d),
        "book_digest_chunk": lambda d: ChunkDigest(**d),
        "book_digest_summary": lambda d: SummaryDigest(**d),
        "book_recommend": lambda d: BookRecommendList(**d),
    }[name]


def _check(name: str, parsed) -> None:
    """关键业务字段存在性断言（解析成功只是下限，字段才决定产出可用）。"""
    if name == "agent_route":
        # 分流是「每轮都跑」的链路，产出必须永远能收敛成一个合法能力名——
        # 模型编一个新名字、confidence 写成中文、args 给成数组，都不能让整轮对话失败
        assert parsed.capability in KNOWN_CAPABILITIES, parsed.capability
        assert isinstance(parsed.args, dict)
        assert 0.0 <= parsed.confidence <= 1.0, parsed.confidence
    elif name == "l1_route":
        assert parsed.decision in ("located", "clarify")
    elif name == "l2_extract":
        assert isinstance(parsed.claims, list)
    elif name == "l2_judge":
        assert parsed.relation in ("矛盾", "互补", "断层", "无关")
        if parsed.relation == "矛盾":
            assert parsed.conflict_type, "relation=矛盾 但 conflict_type 为空"
    elif name == "l2_review":
        assert parsed.relation in ("矛盾", "互补", "断层", "无关")
        if parsed.relation == "矛盾":
            assert parsed.conflict_type, "relation=矛盾 时 conflict_type 为必填"
        assert parsed.uphold_reason, "复核缺维持初判的最强理由"
        assert parsed.overturn_reason, "复核缺推翻初判的最强理由"
    elif name == "l3_analyze":
        assert isinstance(parsed.assignments, list)
    elif name in ("l3_brief", "l3_item"):
        assert isinstance(parsed.questions, list)
    elif name == "l4_plan":
        assert isinstance(parsed.tasks, list)
        assert parsed.tasks, "计划至少应有一个任务"
    elif name == "l4_deviate":
        assert parsed.root_cause and parsed.adjustment
    elif name == "l5_diagnose":
        assert parsed.root_cause and parsed.suggested_action
        assert isinstance(parsed.reasoning_chain, list)
    elif name == "book_digest_chunk":
        assert parsed.gist, "分块要点缺主线概括"
        assert isinstance(parsed.points, list)
    elif name == "book_digest_summary":
        assert parsed.summary, "全书总评为空"
    elif name == "book_recommend":
        assert parsed.items, "推荐书单为空"
        for item in parsed.items:
            assert item.title and item.reason, "推荐条目缺书名或理由"
    else:  # pragma: no cover - 新提示词未登记校验
        raise AssertionError(f"未登记 golden 关键字段断言：{name}")


@pytest.mark.parametrize(
    "name",
    [p.name for p in ALL_PROMPTS],
    ids=[p.name for p in ALL_PROMPTS],
)
def test_golden_outputs_parse(name):
    """回放每个提示词的录制样本：解析成功 + 关键字段齐全。"""
    spec = prompt_by_name(name)
    assert spec is not None
    samples = _load_samples(name)
    assert samples, f"{name} 没有任何 golden 样本，请至少录制一条真实产出"

    validator = _validator_for(name)
    for sample in samples:
        text = sample["text"]
        parsed = parse_structured(text, validator=validator)
        _check(name, parsed)
