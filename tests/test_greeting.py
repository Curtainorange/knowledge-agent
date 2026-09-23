"""主动开场测试（全 Mock，不触网）。

开场是「对话即入口」的第一屏——用户还没说话，副驾先开口。这里钉住三件事：

1. **说什么取决于状态**：有冲突先说冲突、有目标没计划先问要不要拆，
   而不是一段千篇一律的自我介绍。开场套模板，就等于承认它没读用户的数据。
2. **以问句收尾**：主动提问才有引导力；「你可以做 A、B、C」只是把功能清单换个说法。
3. **一个会话只开场一次**：刷新页面就多一句问候，是最容易被一眼看穿的「假智能」。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.agent.conflict_view import ConflictBrief
from app.agent.greeting import UserState, build_opening, collect_state
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
    """每条开场都必须以问句收尾。

    这是「主动引导」的硬约束：陈述句读起来像通知，问句才把人带进对话。
    以后新增分支若写成陈述句，这条会红。
    """
    for state in ALL_STATES:
        text = build_opening(state).strip()
        assert text.endswith("？"), f"{state} → {text!r}"


def test_opening_always_says_who_is_talking():
    for state in ALL_STATES:
        assert "认知副驾" in build_opening(state)


def test_conflicts_outrank_everything_else():
    """有没处理的冲突时先说这件事——它是唯一「事情已经发生、在等你」的状态。"""
    text = build_opening(
        UserState(knowledge_count=9, goal="学英语", has_plan=True, unseen_conflicts=3)
    )
    assert "3 处冲突" in text
    assert "周计划" not in text


def test_goal_without_plan_asks_to_split_it():
    text = build_opening(UserState(goal="三个月掌握数据分析"))
    assert "三个月掌握数据分析" in text
    assert "周计划" in text


def test_goal_with_plan_offers_two_directions():
    text = build_opening(UserState(goal="三个月掌握数据分析", has_plan=True))
    assert "推进" in text
    assert "偏离" in text


def test_knowledge_without_goal_offers_a_scan():
    text = build_opening(UserState(knowledge_count=7))
    assert "7 条" in text
    assert "矛盾" in text


def test_empty_state_teaches_and_asks():
    """全空时既给「怎么用」也给「先做哪件」——它同时是引导与首次教学。"""
    text = build_opening(UserState())
    assert "记" in text
    assert "目标" in text


def test_long_goal_is_truncated():
    """目标描述过长要截断，否则开场就变成了朗读一份书面目标。"""
    text = build_opening(UserState(goal="学" * 200))
    assert "…" in text
    assert len(text) < 120


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
    """同一会话再调一次，不会多出第二句问候——刷新页面不该看到两句开场。"""
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


def test_greeting_reflects_real_knowledge_count(client, session, gateway_calls):
    """开场读的是真实数据：存过内容之后，不该还问「这里还是空的」。"""
    user = sign_in(client, "greet_kn")
    saved = client.post(
        "/api/v1/knowledge/items",
        json={"title": "B+树", "content": "B+树更适合范围查询"},
        headers=user.headers,
    )
    assert saved.status_code == 200, saved.text

    body = _start(client, user.headers)
    assert "1 条" in body["messages"][0]["content"]


# ---- D：开场直击「最该处理的那一处」 ----------------------------------------


def _seed_conflict(session, user_id: str) -> None:
    make_conflict(
        session, user_id, tag="索引",
        statement_a="B+树更适合范围查询",
        statement_b="哈希索引更适合范围查询",
    )


def test_conflict_opening_names_the_conflict_when_details_known():
    """拿到具体要点时要说清是哪两条、哪两个说法——只报数字等于只做了通知。

    看到「3 处冲突」用户仍要自己回去翻是哪三处、哪一点对不上；把双方说法摆出来
    并把问题问出去，才是本项目说的「逼你修正」。
    """
    text = build_opening(UserState(
        unseen_conflicts=3,
        top_conflict=ConflictBrief(
            conflict_id="c1",
            title_a="索引笔记",
            title_b="数据库选型",
            claim_a="B+树更适合范围查询",
            claim_b="哈希索引更适合范围查询",
            conflict_type="立场对立",
            confidence=0.9,
        ),
    ))

    assert "3 处冲突" in text
    assert "索引笔记" in text and "数据库选型" in text
    assert "B+树更适合范围查询" in text
    assert text.endswith("？")
    assert "周计划" not in text


def test_conflict_opening_falls_back_to_count_without_details():
    """没有具体要点时退回只报数量的形态——纯计数构造也必须产出可用开场。"""
    text = build_opening(UserState(unseen_conflicts=3))
    assert "3 处冲突" in text
    assert text.endswith("？")


def test_conflict_opening_truncates_long_text():
    """书名与主张都要截断，否则开场会退化成朗读两句长文。"""
    text = build_opening(UserState(
        unseen_conflicts=1,
        top_conflict=ConflictBrief(
            conflict_id="c1", title_a="甲" * 40, title_b="乙" * 40,
            claim_a="观" * 200, claim_b="点" * 200,
        ),
    ))
    assert "…" in text
    assert text.endswith("？")


def test_collect_state_reads_top_conflict(client, session):
    """collect_state 顺带取「最该处理的那一处」——开场才有东西可直击。"""
    user = sign_in(client, "greet_conflict")
    _seed_conflict(session, user.user_id)

    state = collect_state(session, user_id=user.user_id)

    assert state.unseen_conflicts == 1
    assert state.top_conflict is not None
    assert state.top_conflict.claim_a == "B+树更适合范围查询"


def test_start_uses_conflict_details_in_greeting(client, session, gateway_calls):
    """端到端：有未解冲突时开场直接摆出双方说法，且依然不调模型。"""
    user = sign_in(client, "greet_conflict_detail")
    _seed_conflict(session, user.user_id)

    text = _start(client, user.headers)["messages"][0]["content"]

    assert "1 处冲突" in text
    assert "索引·甲" in text
    assert "B+树更适合范围查询" in text
    assert gateway_calls == []
