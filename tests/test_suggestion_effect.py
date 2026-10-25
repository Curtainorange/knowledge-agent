"""建议的效果回访（B 模块闭环的最后一环）。

「这条不对」只说明他不喜欢什么；「我去做了 / 有用」才说明什么真有用。
这里钉四件事：

- 两步回访的状态正确落在卡片上，并**写回会话**（只改前端内存的话刷新就回退）；
- 事件带 goal / help_type，否则收敛判断匹配不上；
- 「试了没用」进收敛窗口，且文案与「没试就否掉」区分开；
- 「有用了」不进收敛——有用的东西没理由回避。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.agent import suggestion as sugg
from app.agent import turns
from app.agent.cards import suggestion_card
from app.agent.intake import IntakeContext
from app.domain.models.learning_event import LearningEvent
from app.domain.repositories.conversation_repository import ConversationRepository
from app.feedback import events


def _conversation_with_card(session, *, user_id: str = "u1") -> tuple[str, dict]:
    """造一个含建议卡的会话。卡片用真构造器，字段形状与线上一致。"""
    card = suggestion_card(
        key="sugg", state="ok",
        digest_payload={"goal": "problem", "help_type": ["path"]},
        items=[{
            "id": "i1",
            "action": "周会开场让每人说一个自己不确定的判断",
            "why": "先把信息放出来",
            "steps": ["开场 5 分钟", "只记录不评价"],
            "how_to_verify": "数一遍补充信息的发言有几条",
            "basis_item_ids": [],
            "confidence": 0.6,
        }],
        evidence=[], references=[], confidence=0.6, note="",
    )
    repo = ConversationRepository(session, user_id=user_id)
    conv = repo.create(user_id=user_id)
    conv.messages = [
        {"role": "assistant", "content": "建议如下", "source": "suggestion", "card": card},
    ]
    session.commit()
    return conv.id, card


def _event_types(session) -> list[str]:
    return [e.event_type for e in session.scalars(select(LearningEvent)).all()]


def _event_payloads(session, event_type: str) -> list[dict]:
    return [
        e.payload or {} for e in session.scalars(select(LearningEvent)).all()
        if e.event_type == event_type
    ]


def _stored_card(session, conversation_id: str, user_id: str = "u1") -> dict:
    conv = ConversationRepository(session, user_id=user_id).get(conversation_id)
    for raw in conv.messages or []:
        card = raw.get("card") or {}
        if card.get("key") == "sugg":
            return card
    raise AssertionError("会话里找不到建议卡")


# ---------- 两步回访 ----------


def test_done_marks_item_and_records_event(session):
    cid, _ = _conversation_with_card(session)
    reply, card = turns.apply_action(
        session, user_id="u1", conversation_id=cid, card_key="sugg",
        action="suggestion.done", target_id="i1", value="",
    )
    assert card["items"][0]["status"] == "done"
    assert card["items"][0]["effect"] == ""      # 还没问效果
    assert "效果" in reply
    assert events.SUGGESTION_ADOPTED in _event_types(session)

    payload = _event_payloads(session, events.SUGGESTION_ADOPTED)[0]
    # goal / help_type 是收敛判断的匹配键，缺了这条事件就等于没记
    assert payload["goal"] == "problem"
    assert payload["help_type"] == ["path"]
    assert payload["item_id"] == "i1"


def test_effect_records_value(session):
    cid, _ = _conversation_with_card(session)
    reply, card = turns.apply_action(
        session, user_id="u1", conversation_id=cid, card_key="sugg",
        action="suggestion.effect", target_id="i1", value="worked",
    )
    assert card["items"][0]["effect"] == "worked"
    assert card["items"][0]["status"] == "done"   # 直接点效果也视为做过
    assert "接着用" in reply

    payload = _event_payloads(session, events.SUGGESTION_EFFECT)[0]
    assert payload["effect"] == "worked"


def test_unknown_effect_rejected(session):
    cid, _ = _conversation_with_card(session)
    with pytest.raises(turns.ActionError):
        turns.apply_action(
            session, user_id="u1", conversation_id=cid, card_key="sugg",
            action="suggestion.effect", target_id="i1", value="excellent",
        )


def test_done_on_unknown_item_rejected(session):
    cid, _ = _conversation_with_card(session)
    with pytest.raises(turns.ActionError):
        turns.apply_action(
            session, user_id="u1", conversation_id=cid, card_key="sugg",
            action="suggestion.done", target_id="ghost", value="",
        )


def test_state_is_written_back_to_conversation(session):
    """只改返回的卡、不写回会话的话，用户刷新一次就回到「我去做了」之前。"""
    cid, _ = _conversation_with_card(session)
    turns.apply_action(
        session, user_id="u1", conversation_id=cid, card_key="sugg",
        action="suggestion.done", target_id="i1", value="",
    )
    stored = _stored_card(session, cid)
    assert stored["items"][0]["status"] == "done"


# ---------- 收敛：「试了没用」比「没试就否掉」更硬 ----------


def _ctx(**over) -> IntakeContext:
    base = dict(question="怎么把极度透明用到周会上", goal="problem",
                self_level="once", help_type=["path"])
    base.update(over)
    ctx = IntakeContext(**base)
    ctx.refresh_missing()
    return ctx


def test_effect_none_restrains_same_kind(session):
    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_EFFECT,
        payload={"goal": "problem", "help_type": ["path"], "effect": "none"},
    )
    reason = sugg._restrained_reason(session, user_id="u1", ctx=_ctx())
    assert "试过" in reason and "没用" in reason


def test_effect_worked_does_not_restrain(session):
    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_EFFECT,
        payload={"goal": "problem", "help_type": ["path"], "effect": "worked"},
    )
    assert sugg._restrained_reason(session, user_id="u1", ctx=_ctx()) == ""


def test_partial_effect_does_not_restrain(session):
    """「一般」意味着方向对了一半，正是最该继续给的方向。"""
    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_EFFECT,
        payload={"goal": "problem", "help_type": ["path"], "effect": "partial"},
    )
    assert sugg._restrained_reason(session, user_id="u1", ctx=_ctx()) == ""


def test_effect_none_for_other_goal_not_restrained(session):
    events.record(
        session, user_id="u1", event_type=events.SUGGESTION_EFFECT,
        payload={"goal": "exam", "help_type": ["path"], "effect": "none"},
    )
    assert sugg._restrained_reason(session, user_id="u1", ctx=_ctx()) == ""
