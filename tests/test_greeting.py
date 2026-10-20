"""主动开场测试（全 Mock，不触网）。

开场是「对话即入口」的第一屏——用户还没说话，副驾先开口。这里钉住四件事：

1. **开场只问候 + 确认，不推动作**（B 模块的核心改造）。原实现按「冲突 / 目标 /
   计划 / 积累」分五支，每支都直接问"要不要拆计划""要不要扫冲突"——那是在用户
   还没说要什么时替他决定下一步。现在这些分支全删了，改为一张需求确认卡。
   所以本文件里有几条**反断言**：开场不许出现"周计划""偏离""扫一遍"。
2. **以问句收尾**：陈述句读起来像通知，问句才把人带进对话。
3. **一个会话只开场一次**：刷新页面就多一句问候，是最容易被一眼看穿的「假智能」。
4. **开场零模型调用**：不给"打开对话框"这个动作绑成本。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.greeting import UserState, collect_state, opening_text
from app.agent.intake import IntakeContext
from app.api import deps
from app.domain.models.conversation import Conversation
from app.domain.models.learning_goal import LearningGoal
from app.domain.models.learning_plan import LearningPlan
from app.llm.completion import Completion
from app.llm.gateway import ModelGateway
from app.llm.provider import LLMProvider
from app.main import app
from tests.conflict_fixtures import make_conflict
from tests.helpers import sign_in

# 所有文案分支都要被这些状态覆盖到（加分支时同时补进来）
ALL_STATES = [
    UserState(),                                                      # 全空
    UserState(knowledge_count=5),                                     # 有积累、没目标
    UserState(goal="三个月掌握数据分析"),                                # 有目标、没计划
    UserState(goal="三个月掌握数据分析", has_plan=True),                 # 目标计划都在
    UserState(unseen_conflicts=3),                                    # 有未处理冲突
]


@pytest.fixture()
def gateway_calls():
    """记录模型调用（开场必须零调用）。"""

    calls: list[str] = []

    class _Recorder(LLMProvider):
        def chat(
            self, *, model, reasoning, messages, tools=None, response_format=None, task_type="default"
        ) -> Completion:
            calls.append(task_type)
            return Completion(
                text="（开场不该调模型）", prompt_tokens=0, completion_tokens=0,
                finish_reason="stop", model=model, reasoning=reasoning,
            )

    app.dependency_overrides[deps.get_gateway] = lambda: ModelGateway(provider=_Recorder())
    yield calls
    app.dependency_overrides.pop(deps.get_gateway, None)


# ---- 文案（纯函数，不碰数据库）---------------------------------------------


def test_every_opening_ends_with_a_question():
    """每条开场都必须以问句收尾——主动引导的硬约束。"""
    for state in ALL_STATES:
        text = opening_text(state).strip()
        assert text.endswith("？"), f"{state} → {text!r}"


def test_opening_always_says_who_is_talking():
    for state in ALL_STATES:
        assert "认知副驾" in opening_text(state)


@pytest.mark.parametrize(
    "state",
    [
        UserState(goal="三个月掌握数据分析"),
        UserState(goal="三个月掌握数据分析", has_plan=True),
        UserState(knowledge_count=7),
    ],
)
def test_opening_does_not_push_actions(state):
    """有目标 / 有积累时**不许**直接推动作。

    这三条以前是「要不要拆周计划」「接着走还是看偏离」「要不要扫一遍」，
    正是会议纪要批评的「未确认就给建议」。现在必须全部消失。
    """
    text = opening_text(state)
    for banned in ("周计划", "拆成", "偏离", "扫一遍", "扫出来"):
        assert banned not in text, f"{state} → {text!r} 仍含 {banned}"


def test_conflicts_are_a_todo_not_a_suggestion():
    """冲突保留，但只能以「待办提示」出现：它是事情在等你，不是我建议你做。"""
    text = opening_text(UserState(knowledge_count=9, unseen_conflicts=3))
    assert "3 处观点冲突" in text
    assert text.endswith("？")


def test_opening_does_not_recite_goal_text():
    """目标是确认项，不该被开场朗读一遍。"""
    text = opening_text(UserState(goal="非常长的目标" * 20))
    assert "非常长的目标" not in text


def test_opening_mentions_inferred_book():
    """有推断出在读哪本时，开场把推断摆出来（用户只需确认）。"""
    ctx = IntakeContext(
        book={"book_id": "b1", "title": "原则", "source": "reader", "progress": 0.4, "chapter": "第3章"}
    )
    text = opening_text(UserState(knowledge_count=5), ctx)
    assert "《原则》" in text and "第3章" in text


def test_empty_state_teaches_and_asks():
    """全空时给两个入口（记一条 / 同步）——它同时是引导与首次教学。"""
    text = opening_text(UserState())
    assert "记" in text
    assert "同步" in text
    assert text.endswith("？")


# ---- 状态读取（真实库）-----------------------------------------------------


def test_collect_state_reads_goal_without_plan(client, session):
    user = sign_in(client, "greet_state")
    session.add(LearningGoal(user_id=user.user_id, description="三个月掌握数据分析"))
    session.flush()

    state = collect_state(session, user_id=user.user_id)
    assert state.goal == "三个月掌握数据分析"
    assert state.has_plan is False
    assert state.knowledge_count == 0
    assert state.unseen_conflicts == 0


def test_collect_state_sees_the_existing_plan(client, session):
    user = sign_in(client, "greet_plan")
    goal = LearningGoal(user_id=user.user_id, description="学英语")
    session.add(goal)
    session.flush()
    session.add(LearningPlan(user_id=user.user_id, goal_id=goal.id, content={}, version=1))
    session.flush()

    state = collect_state(session, user_id=user.user_id)
    assert state.goal == "学英语"
    assert state.has_plan is True


def test_collect_state_is_scoped_to_the_user(client, session):
    """状态必须按用户隔离——两个账号共用一句开场是不可接受的。"""
    mine = sign_in(client, "greet_mine")
    theirs = sign_in(client, "greet_theirs")
    session.add(LearningGoal(user_id=theirs.user_id, description="别人的目标"))
    session.flush()

    assert collect_state(session, user_id=mine.user_id).goal == ""
    assert collect_state(session, user_id=theirs.user_id).goal == "别人的目标"


# ---- 端点：开启 / 恢复会话 --------------------------------------------------


def _start(client: TestClient, headers: dict, conversation_id: str | None = None) -> dict:
    resp = client.post(
        "/api/v1/agent/start",
        json={"conversation_id": conversation_id},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_start_opens_with_greeting_and_persists_it(client, session, gateway_calls):
    """新会话：副驾先开口，且开场消息**落库**——刷新页面它还在。"""
    user = sign_in(client, "greet_start")
    body = _start(client, user.headers)

    assert len(body["messages"]) == 1
    first = body["messages"][0]
    assert first["role"] == "assistant"
    assert "认知副驾" in first["content"]

    session.expire_all()
    conv = session.get(Conversation, body["conversation_id"])
    assert conv is not None
    assert len(conv.messages or []) == 1


def test_start_costs_nothing(client, session, gateway_calls):
    """开场**不调模型**：不该给「打开对话框」这个动作绑上成本与延迟。"""
    user = sign_in(client, "greet_free")
    _start(client, user.headers)
    assert gateway_calls == []


def test_start_does_not_repeat_greeting(client, session, gateway_calls):
    """同一会话再调一次，不会多出第二句问候。"""
    user = sign_in(client, "greet_twice")
    first = _start(client, user.headers)
    cid = first["conversation_id"]

    again = _start(client, user.headers, cid)
    assert again["conversation_id"] == cid
    assert len(again["messages"]) == 1


def test_start_returns_existing_history_instead_of_greeting(client, session, gateway_calls):
    """已有对话的会话：`/start` 就是「恢复历史」，绝不插开场。"""
    user = sign_in(client, "greet_history")
    chat = client.post(
        "/api/v1/agent/chat", json={"message": "你好呀"}, headers=user.headers
    )
    assert chat.status_code == 200, chat.text
    cid = chat.json()["conversation_id"]

    body = _start(client, user.headers, cid)
    assert body["conversation_id"] == cid
    contents = [m["content"] or "" for m in body["messages"]]
    assert contents[0] == "你好呀"
    assert not any("我是你的认知副驾" in c for c in contents)


def test_start_never_leaks_another_users_conversation(client, session, gateway_calls):
    """拿别人的 / 过期的会话 id 进来不能 500——静默开一个新会话（含开场）。"""
    owner = sign_in(client, "greet_owner")
    intruder = sign_in(client, "greet_intruder")
    theirs = _start(client, owner.headers)

    body = _start(client, intruder.headers, theirs["conversation_id"])
    assert body["conversation_id"] != theirs["conversation_id"]
    assert "认知副驾" in body["messages"][0]["content"]


# ---- 确认卡随开场一起发 -----------------------------------------------------


def _first_card(body: dict) -> dict | None:
    for m in body["messages"]:
        card = m.get("card") or {}
        if card.get("kind"):
            return card
    return None


def test_start_attaches_intake_card_when_library_has_content(client, session, gateway_calls):
    """库里有内容时，开场带一张需求确认卡（≤2 题，全部可点选）。"""
    user = sign_in(client, "greet_card")
    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "B+树", "content": "B+树更适合范围查询"},
        headers=user.headers,
    )
    assert saved.status_code == 200, saved.text

    body = _start(client, user.headers)
    card = _first_card(body)
    assert card is not None and card["kind"] == "reading_intake"
    assert 1 <= len(card["questions"]) <= 2
    assert gateway_calls == []


def test_start_skips_intake_card_when_library_is_empty(client, session, gateway_calls):
    """库空时不发确认卡：没有内容可确认，问五件事也没用。"""
    user = sign_in(client, "greet_empty_card")
    body = _start(client, user.headers)
    assert _first_card(body) is None
    assert "库" in body["messages"][0]["content"]


def test_start_persists_reading_context(client, session, gateway_calls):
    """开场的推断要落进会话上下文，刷新回来不必重说在读什么。"""
    user = sign_in(client, "greet_ctx")
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "原则", "content": "极度透明意味着什么"},
        headers=user.headers,
    )
    body = _start(client, user.headers)

    session.expire_all()
    conv = session.get(Conversation, body["conversation_id"])
    ctx = conv.reading_context or {}
    assert ctx.get("status") == "collecting"
    assert "question" in (ctx.get("missing") or [])


# ---- 冲突：仍在开场里，但只报数量 -------------------------------------------


def _seed_conflict(session, user_id: str) -> None:
    make_conflict(
        session, user_id, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )


def test_start_uses_conflict_count_not_claims(client, session, gateway_calls):
    """端到端：有未解冲突时只报数量，**不摆双方主张**。

    摆主张等于替用户把议题定了——那是建议行为，要等确认之后。
    """
    user = sign_in(client, "greet_conflict_detail")
    _seed_conflict(session, user.user_id)
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "索引", "content": "B+树更适合范围查询"},
        headers=user.headers,
    )

    text = _start(client, user.headers)["messages"][0]["content"]

    assert "1 处观点冲突" in text
    assert "哈希索引更适合范围查询" not in text
    assert gateway_calls == []
