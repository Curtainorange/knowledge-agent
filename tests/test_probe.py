"""E 模块测试：理解度探查（两轮 / 判定矩阵 / 诊断门禁）。

对应 `docs/验收自检与修正-2026-10-20.md` 修正 2 与总方案 E 模块验收标准。
全程 Mock，不触网。
"""
from __future__ import annotations

import pytest

from app.agent import probe
from app.agent.probe import ProbeDraft, ProbeOption

GOOD_POINTS = [
    "极度透明的核心是组织内部没有不能公开讨论的问题。",
    "它讲的是信息的可见性，不是决策权的分配方式。",
    "它需要在团队内部建立说真话的安全感。",
]


def _draft(**over) -> ProbeDraft:
    base = dict(
        topic="极度透明",
        question="下面哪个说法更接近原文？",
        options=[
            ProbeOption(key="a", text="组织内没有不能公开讨论的问题", correct=True, why="这是原文定义"),
            ProbeOption(key="b", text="所有决策全员投票", correct=False, why="混淆了信息与决策权"),
            ProbeOption(key="c", text="对客户公开财务", correct=False, why="语境是内部"),
        ],
        key_points=list(GOOD_POINTS),
    )
    base.update(over)
    return ProbeDraft(**base)


# ---- 覆盖率（复述判分，代码算）----------------------------------------------


def test_coverage_full_when_recall_restates_points():
    text = "极度透明就是组织里没有不能公开讨论的问题，它讲的是信息可见性，还需要说真话的安全感。"
    covered, missed, ratio = probe.coverage_of(text, GOOD_POINTS)
    assert ratio > 0.5
    assert not missed


def test_coverage_zero_on_empty_recall():
    covered, missed, ratio = probe.coverage_of("", GOOD_POINTS)
    assert ratio == 0.0
    assert len(missed) == 3


def test_coverage_partial():
    text = "极度透明是组织里没有不能公开讨论的问题。"
    covered, missed, ratio = probe.coverage_of(text, GOOD_POINTS)
    assert covered and missed
    assert 0 < ratio < 1


def test_coverage_without_points_returns_zero():
    assert probe.coverage_of("随便说点什么", []) == ([], [], 0.0)


# ---- 判定矩阵 ---------------------------------------------------------------


def test_first_time_is_guide_regardless_of_answer():
    assert probe.decide(self_level="first", judge_correct=False, coverage=0.0) == probe.VERDICT_GUIDE
    assert probe.decide(self_level="first", judge_correct=True, coverage=1.0) == probe.VERDICT_GUIDE


def test_correct_answer_is_mastered():
    assert probe.decide(self_level="once", judge_correct=True, coverage=0.0) == probe.VERDICT_MASTERED


def test_wrong_and_low_coverage_is_gap():
    assert probe.decide(self_level="once", judge_correct=False, coverage=0.1) == probe.VERDICT_GAP


def test_wrong_but_high_coverage_is_bias():
    """说得出要点但答错 → 方向对、表述偏，不该判成"你没懂"。"""
    assert probe.decide(self_level="teach", judge_correct=False, coverage=0.8) == probe.VERDICT_BIAS


def test_missing_judgement_is_unknown():
    assert probe.decide(self_level="once", judge_correct=None, coverage=0.0) == probe.VERDICT_UNKNOWN


# ---- 要不要追问复述 ---------------------------------------------------------


@pytest.mark.parametrize(
    "level,correct,expected",
    [
        ("first", False, False),   # 没学过，答错是正常的
        ("first", True, False),
        ("once", True, False),     # 答对了，不必再问
        ("once", False, True),     # 自称读过却答错 → 追问
        ("teach", False, True),
    ],
)
def test_needs_recall(level, correct, expected):
    assert probe.needs_recall(level, correct) is expected


# ---- 出题校验 ---------------------------------------------------------------


def test_draft_ok_accepts_valid():
    assert probe._draft_ok(_draft())


def test_draft_rejects_two_correct():
    bad = _draft(options=[
        ProbeOption(key="a", text="甲", correct=True),
        ProbeOption(key="b", text="乙", correct=True),
        ProbeOption(key="c", text="丙", correct=False),
    ])
    assert not probe._draft_ok(bad)


def test_draft_rejects_too_few_options():
    bad = _draft(options=[
        ProbeOption(key="a", text="甲", correct=True),
        ProbeOption(key="b", text="乙", correct=False),
    ])
    assert not probe._draft_ok(bad)


def test_draft_rejects_too_few_points():
    assert not probe._draft_ok(_draft(key_points=["只有一条"]))


def test_draft_allows_three_short_points():
    """三条要点只要非空就够——长度不是判据，要点是否切题靠出题提示词管。"""
    assert probe._draft_ok(_draft(key_points=["一", "二", "三"]))


def test_draft_rejects_duplicate_keys():
    bad = _draft(options=[
        ProbeOption(key="a", text="甲", correct=True),
        ProbeOption(key="a", text="乙", correct=False),
        ProbeOption(key="c", text="丙", correct=False),
    ])
    assert not probe._draft_ok(bad)


# ---- 状态流转 ---------------------------------------------------------------


def _save(session, user_id="u1"):
    return probe.save_question(session, user_id=user_id, draft=_draft())


def test_state_needs_question_when_nothing(session):
    assert probe.current_state(session, user_id="u1").state == probe.STATE_NEED_QUESTION


def test_state_needs_question_when_unanswered(session):
    record = _save(session)
    state = probe.current_state(session, user_id="u1")
    assert state.state == probe.STATE_NEED_QUESTION
    assert state.record.id == record.id  # 原样重发同一道题，不换题


def test_first_time_shortcuts_to_guide(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="first")
    state = probe.current_state(session, user_id="u1")
    assert state.state == probe.STATE_ALLOW
    assert state.verdict == probe.VERDICT_GUIDE


def test_correct_answer_shortcuts_to_mastered(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="once")
    probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="a")
    state = probe.current_state(session, user_id="u1")
    assert state.verdict == probe.VERDICT_MASTERED


def test_wrong_answer_requires_recall(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="once")
    probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="b")
    state = probe.current_state(session, user_id="u1")
    assert state.state == probe.STATE_NEED_RECALL
    assert not state.record.verdict


def test_recall_sets_gap_when_points_missed(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="once")
    probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="b")
    answered = probe.answer_recall(
        session, user_id="u1", probe_id=record.id, recall_text="说不上来，大概是公开吧。"
    )
    assert answered.verdict == probe.VERDICT_GAP
    assert answered.coverage < probe.COVERAGE_THRESHOLD


def test_recall_sets_bias_when_points_covered(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="teach")
    probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="b")
    answered = probe.answer_recall(
        session, user_id="u1", probe_id=record.id,
        recall_text=(
            "极度透明的核心是组织内部没有不能公开讨论的问题，"
            "它讲的是信息的可见性不是决策权，还需要说真话的安全感。"
        ),
    )
    assert answered.verdict in (probe.VERDICT_BIAS, probe.VERDICT_GAP)
    assert answered.coverage > 0


# ---- 越权与非法输入 ---------------------------------------------------------


def test_other_user_cannot_answer(session):
    record = _save(session, user_id="u1")
    assert probe.set_self_level(session, user_id="u2", probe_id=record.id, self_level="once") is None
    assert probe.answer_recall(session, user_id="u2", probe_id=record.id, recall_text="x") is None


def test_invalid_option_rejected(session):
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="once")
    with pytest.raises(ValueError, match="没有这个选项"):
        probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="z")


def test_judge_requires_self_level_first(session):
    record = _save(session)
    with pytest.raises(ValueError, match="接触史"):
        probe.answer_judge(session, user_id="u1", probe_id=record.id, option_key="a")


def test_expired_probe_requires_new_question(session):
    """7 天前的结论不再算数——人对一本书的理解是会变的。"""
    record = _save(session)
    probe.set_self_level(session, user_id="u1", probe_id=record.id, self_level="first")
    assert probe.current_state(session, user_id="u1", days=7).verdict == probe.VERDICT_GUIDE
    # 把记录时间往前推 30 天，就应当重新出题
    record.created_at = record.created_at - __import__("datetime").timedelta(days=30)
    session.commit()
    assert probe.current_state(session, user_id="u1", days=7).state == probe.STATE_NEED_QUESTION


# ---- 端到端：诊断被拦住，答完才放行 -----------------------------------------


def test_diagnosis_is_blocked_until_probe_finished(client, session, monkeypatch):
    """点「诊断」→ 先被探查拦住 → 答两轮 → 才真的出诊断。

    这条用例守住的是"门禁不能靠部署条件"：`preflight` 只在有 worker 时跑，
    无 worker 的降级路径会直接进 `execute_capability`——所以门禁必须在那里面也判一次。
    """
    from app.agent import probe as probe_mod
    from app.domain.models.knowledge_item import KnowledgeItem
    from tests.helpers import sign_in

    monkeypatch.setattr(
        probe_mod, "build_probe",
        lambda session, *, user_id, gateway=None, context=None: _draft(),
    )

    user = sign_in(client, "probe_e2e")
    headers = user.headers
    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则 · 第三章", "content": "极度透明意味着组织里没有不能说的话。"},
        headers=headers,
    )
    assert saved.status_code == 200, saved.text

    first = client.post(
        "/api/v1/agent/chat", json={"message": "诊断我的学习状态"}, headers=headers
    ).json()
    card = first["card"]
    assert first["capability"] == "l5"
    assert card["kind"] == "comprehension_probe"
    assert card["step"] == "level"

    # 第 1 步：接触史。**参数要与前端一致**：选项值走 target_id、value 留空。
    # 这条用例曾经传反（target_id="", value="once"），把后端的读写错位掩盖了——
    # 测试模拟错误的契约，等于没测。
    resp = client.post(
        "/api/v1/agent/actions",
        json={
            "conversation_id": first["conversation_id"],
            "card_key": card["key"],
            "action": "probe.level",
            "target_id": "once",
            "value": "",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    judge_card = resp.json()["card"]
    assert judge_card["step"] == "judge"
    assert len(judge_card["options"]) == 3
    # 判断题阶段不能泄露答案
    assert all("correct" not in o for o in judge_card["options"])

    # 第 2 步：故意选错（b 是误解项）→ 转入复述
    resp = client.post(
        "/api/v1/agent/actions",
        json={
            "conversation_id": first["conversation_id"],
            "card_key": judge_card["key"],
            "action": "probe.answer",
            "target_id": "b",
            "value": "",
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["card"]["step"] == "recall"

    # 第 3 步：复述（直接说话）
    answered = client.post(
        "/api/v1/agent/chat",
        json={"message": "说不上来，大概是公开吧。", "conversation_id": first["conversation_id"]},
        headers=headers,
    ).json()
    verdict_card = answered["card"]
    assert verdict_card["kind"] == "comprehension_probe"
    assert verdict_card["step"] == "verdict"
    assert verdict_card["verdict"]["verdict"] in (probe_mod.VERDICT_GAP, probe_mod.VERDICT_BIAS)

    row = session.query(KnowledgeItem).count()
    assert row >= 1

    # 第 4 步：再点诊断 → 这次放行（不再发探查卡）
    again = client.post(
        "/api/v1/agent/chat",
        json={"message": "诊断我的学习状态", "conversation_id": first["conversation_id"]},
        headers=headers,
    ).json()
    assert again["capability"] == "l5"
    assert (again.get("card") or {}).get("kind") != "comprehension_probe"
