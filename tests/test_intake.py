"""B 模块测试：需求确认状态机（intake）。

对应 `docs/B模块-主动建议逻辑设计-2026-10-20.md` 的 AT-B-01~05 与 AT-B-13/14。
全部为纯函数与本地查询，不调模型。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import intake
from app.agent.intake import IntakeContext
from app.domain.models.book import Book
from app.domain.models.knowledge_item import KnowledgeItem
from app.ingestion.service import IngestionService
from app.retrieval.embedding import HashEmbedding


def _filled_ctx() -> IntakeContext:
    ctx = IntakeContext()
    ctx.question = "怎么把极度透明用到周会上"
    ctx.goal = "problem"
    ctx.self_level = "once"
    ctx.help_type = ["path"]
    ctx.refresh_missing()
    return ctx


# ---- 基础状态 ---------------------------------------------------------------


def test_empty_context_misses_four_slots():
    ctx = IntakeContext()
    ctx.refresh_missing()
    assert ctx.status == intake.STATUS_COLLECTING
    assert ctx.missing == ["question", "goal", "self_level", "help_type"]
    assert ctx.is_ready is False


def test_all_slots_filled_becomes_ready():
    ctx = _filled_ctx()
    assert ctx.missing == []
    assert ctx.is_ready is True
    assert ctx.status == intake.STATUS_READY


def test_blank_question_does_not_count():
    ctx = IntakeContext(question="   ")
    ctx.refresh_missing()
    assert "question" in ctx.missing


def test_goal_other_requires_note():
    ctx = IntakeContext(goal="other")
    ctx.refresh_missing()
    assert "goal" in ctx.missing
    ctx.goal_note = "想给产品团队做内部分享"
    ctx.refresh_missing()
    assert "goal" not in ctx.missing


def test_apply_answer_increments_round_and_fills():
    ctx = IntakeContext()
    ctx.refresh_missing()
    intake.apply_answer(ctx, "question", "怎么落地")
    assert ctx.round == 1
    assert "question" not in ctx.missing


def test_apply_answer_rejects_unknown_enum():
    ctx = IntakeContext()
    with pytest.raises(ValueError):
        intake.apply_answer(ctx, "self_level", "大师")


def test_apply_answer_overrides_auto_inference():
    ctx = IntakeContext(goal="concept")
    ctx.refresh_missing()
    intake.apply_answer(ctx, "goal", "problem")
    assert ctx.goal == "problem"


def test_help_type_keeps_at_most_two():
    ctx = IntakeContext()
    intake.apply_answer(ctx, "help_type", ["path", "explain", "critique"])
    assert ctx.help_type == ["path", "explain"]


# ---- 跳过 -------------------------------------------------------------------


def test_skip_three_times_enters_reduced_mode():
    ctx = IntakeContext()
    ctx.refresh_missing()
    for _ in range(3):
        intake.mark_skip(ctx)
    assert ctx.reduced_mode is True
    # 精简模式只问 question，其余不再追问
    assert ctx.missing == ["question"]


def test_answer_after_skip_resets_skip_counter():
    ctx = IntakeContext()
    ctx.refresh_missing()
    intake.mark_skip(ctx)
    intake.apply_answer(ctx, "question", "想搞懂索引")
    assert ctx.skips == 0 and ctx.skipped is False


# ---- 出题 -------------------------------------------------------------------


def test_next_questions_capped_at_two():
    ctx = IntakeContext()
    ctx.refresh_missing()
    questions = intake.next_questions(ctx)
    assert len(questions) == 2


def test_next_questions_follows_slot_order():
    ctx = IntakeContext()
    ctx.refresh_missing()
    slots = [q["slot"] for q in intake.next_questions(ctx)]
    assert slots == ["question", "goal"]


def test_next_questions_never_repeat_answered():
    ctx = IntakeContext(question="怎么落地")
    ctx.refresh_missing()
    slots = [q["slot"] for q in intake.next_questions(ctx)]
    assert "question" not in slots


def test_next_questions_empty_when_ready():
    assert intake.next_questions(_filled_ctx()) == []


def test_question_card_is_text_with_other_allowed():
    ctx = IntakeContext()
    ctx.refresh_missing()
    first = intake.next_questions(ctx)[0]
    assert first["kind"] == "text"
    assert first["allow_other"] is True


# ---- 槽位抽取 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我在准备考试", "exam"),
        ("要写一篇公众号文章", "output"),
        ("这个怎么用在实际工作里", "problem"),
        ("想搞懂它到底是什么", "concept"),
        ("就随便看看", "casual"),
    ],
)
def test_extract_goal(text, expected):
    assert intake.extract_slots(text).get("goal") == expected


def test_extract_goal_prefers_specific_over_general():
    """「考试」比「理解」更具体：同时出现时按前者，否则会漏掉真正的目的。"""
    assert intake.extract_slots("想搞懂概念，为了考试").get("goal") == "exam"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我第一次接触这块", "first"),
        ("读过一遍但记不全", "once"),
        ("能给团队讲明白", "teach"),
    ],
)
def test_extract_self_level(text, expected):
    assert intake.extract_slots(text).get("self_level") == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("想让我把它讲清楚", "explain"),
        ("帮我找找库里怎么说", "evidence"),
        ("给我具体步骤", "path"),
        ("帮我挑毛病", "critique"),
        ("只回答问题别扩展", "answer_only"),
    ],
)
def test_extract_help_type(text, expected):
    assert expected in intake.extract_slots(text)["help_type"]


def test_extract_book_and_progress():
    slots = intake.extract_slots("我在读《原则》第3章，读到 40%")
    assert slots["book_title"] == "原则"
    assert slots["progress"]["chapter"] == "第3章"
    assert slots["progress"]["progress"] == pytest.approx(0.4)


def test_extract_question_strips_leading_noise():
    slots = intake.extract_slots("我想知道怎么把它用到周会上")
    assert slots["question"] == "怎么把它用到周会上"


def test_extract_question_keeps_original_wording():
    """问题必须是用户原话，不改写——限定条件一改，建议就跑偏。"""
    text = "极度透明在我们团队会不会伤到人"
    assert intake.extract_slots(text)["question"] == text


def test_extract_empty_text():
    assert intake.extract_slots("   ") == {}


def test_no_false_positive_on_unrelated_text():
    """没有明确短语时不要瞎抽。"""
    slots = intake.extract_slots("今天天气不错")
    assert "goal" not in slots
    assert "self_level" not in slots
    assert "help_type" not in slots
    assert slots["question"] == "今天天气不错"


# ---- merge 语义 --------------------------------------------------------------


def test_merge_does_not_override_answered_slot():
    ctx = IntakeContext(goal="concept")
    intake.merge_slots(ctx, {"goal": "problem"})
    assert ctx.goal == "concept"


def test_merge_fills_empty_slot():
    ctx = IntakeContext()
    intake.merge_slots(ctx, {"goal": "problem", "question": "怎么落地"})
    assert ctx.goal == "problem"
    assert ctx.question == "怎么落地"


def test_merge_user_progress_overrides_system_inference():
    ctx = IntakeContext(
        book={"book_id": "b1", "title": "原则", "source": "reader", "progress": 0.8, "chapter": ""},
        progress_source="system",
    )
    intake.merge_slots(ctx, {"progress": {"progress": 0.1, "chapter": "第1章"}})
    assert ctx.progress_source == "user_corrected"
    assert ctx.book["progress"] == pytest.approx(0.1)


# ---- 文案 -------------------------------------------------------------------


def test_opening_gives_no_action_suggestion():
    """开场只能问候 + 确认，不能推动作（AT-B-01）。"""
    ctx = IntakeContext()
    text = intake.opening_text(ctx)
    for banned in ("要不要扫", "拆成", "生成简报", "先扫一遍", "建议你"):
        assert banned not in text
    assert "认知副驾" in text


def test_opening_mentions_inferred_book_and_chapter():
    ctx = IntakeContext(
        book={"book_id": "b1", "title": "原则", "source": "reader", "progress": 0.4, "chapter": "第3章"}
    )
    text = intake.opening_text(ctx)
    assert "《原则》" in text and "第3章" in text


def test_opening_keeps_conflict_as_todo_not_suggestion():
    ctx = IntakeContext()
    text = intake.opening_text(ctx, unseen_conflicts=2)
    assert "2 处观点冲突" in text


def test_confirm_text_only_restates_answered():
    ctx = IntakeContext(question="怎么落地", goal="problem")
    ctx.refresh_missing()
    text = intake.confirm_text(ctx)
    assert "怎么落地" in text
    # 没答的不许替用户补
    assert "能讲给别人" not in text


def test_confirm_text_when_nothing_answered():
    assert "先说说" in intake.confirm_text(IntakeContext())


# ---- 序列化 -----------------------------------------------------------------


def test_roundtrip_serialization():
    ctx = _filled_ctx()
    ctx.book = {"book_id": "b1", "title": "原则", "source": "reader", "progress": 0.4, "chapter": "第3章"}
    restored = IntakeContext.from_dict(ctx.to_dict())
    assert restored.question == ctx.question
    assert restored.goal == ctx.goal
    assert restored.self_level == ctx.self_level
    assert restored.help_type == ctx.help_type
    assert restored.book["title"] == "原则"
    assert restored.missing == []


def test_from_dict_tolerates_garbage():
    ctx = IntakeContext.from_dict({"goal": "不存在的枚举", "self_level": 42, "help_type": "abc"})
    assert ctx.goal == ""
    assert ctx.self_level == ""
    assert ctx.help_type == []
    assert ctx.missing  # 仍会重新问


def test_from_dict_none_returns_empty_context():
    ctx = IntakeContext.from_dict(None)
    assert ctx.missing == ["question", "goal", "self_level", "help_type"]


# ---- 进度推断 ---------------------------------------------------------------


def _book(session, *, user_id="u1", progress=0.4, current_char=12, title="原则"):
    book = Book(
        user_id=user_id,
        title=title,
        author="",
        format="txt",
        file_path="x.txt",
        chapters=[
            {"index": 0, "title": "第一章", "char_start": 0, "char_end": 10},
            {"index": 1, "title": "第二章", "char_start": 10, "char_end": 100},
        ],
        full_text="x" * 100,
        total_chars=100,
        current_char=current_char,
        read_progress=progress,
    )
    session.add(book)
    session.commit()
    return book


def test_infer_from_inner_reader_progress(session):
    _book(session)
    result = intake.infer_reading_target(session, user_id="u1")
    assert result["basis"] == "read_progress"
    assert result["book"]["source"] == "reader"
    assert result["book"]["chapter"] == "第二章"
    assert result["book"]["progress"] == pytest.approx(0.4)


def test_infer_skips_finished_book(session):
    _book(session, progress=1.0)
    assert intake.infer_reading_target(session, user_id="u1")["basis"] == "none"


def test_infer_from_weread_highlight_has_no_percent(session):
    """微信读书同步不带全书进度：只推断到章节，progress 必须为 None。"""
    IngestionService(session, HashEmbedding()).add_knowledge(
        user_id="u1", title="原则 · 第三章", content="极度透明意味着什么", source="weread"
    )
    item = session.query(KnowledgeItem).first()
    item.source_locator = {"book_id": "wr1", "book_title": "原则", "chapter_title": "第三章"}
    session.commit()

    result = intake.infer_reading_target(session, user_id="u1")
    assert result["basis"] == "weread_highlight"
    assert result["book"]["progress"] is None
    assert result["book"]["chapter"] == "第三章"


def test_infer_returns_none_when_nothing(session):
    assert intake.infer_reading_target(session, user_id="nobody")["basis"] == "none"


def test_load_attaches_inference_once(session):
    _book(session)
    conv = SimpleNamespace(reading_context=None)
    ctx = intake.load(session, conversation=conv, user_id="u1")
    assert ctx.book is not None
    # 再读一次不应该覆盖用户已确认的内容
    ctx.question = "怎么落地"
    intake.save(session, conversation=conv, ctx=ctx)
    again = intake.load(session, conversation=conv, user_id="u1")
    assert again.question == "怎么落地"
