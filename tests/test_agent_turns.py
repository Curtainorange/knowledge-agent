"""对话入口的异步回合与卡片内操作回流测试（全 Mock，不触网）。

两条最值得钉死的不变量：

1. **没有 worker 时不能异步**。异步依赖后台线程干活，`worker_enabled=False`
   时若还发 pending 卡，那张卡会永远转圈——用户既看不到结果也等不到失败。
   所以此时必须退回同步执行。反过来，worker 开着时也必须真的走后台，
   否则「异步」只是嘴上说说。
2. **卡片状态必须与数据库一致**。消息里存的是快照，而冲突的 user_state、
   诊断的 status 是会变的；读取时若不刷新，重新打开页面会看到已经忽略过的
   冲突又变成「待处理」——卡片在说谎。
"""
from __future__ import annotations

from sqlalchemy import select

from app.agent import turns
from app.core.config import settings
from app.domain.models.book import Book
from app.domain.models.conversation import Conversation
from app.domain.models.cognitive_diagnosis import CognitiveDiagnosis
from app.domain.models.conflict import Conflict
from app.domain.models.knowledge_item import KnowledgeItem
from app.domain.models.task_run import TaskRun
from app.domain.repositories.conflict_repository import ConflictRepository
from app.domain.repositories.conversation_repository import ConversationRepository
from app.domain.repositories.cognitive_diagnosis_repository import CognitiveDiagnosisRepository
from app.domain.repositories.knowledge_repository import KnowledgeRepository
from tests.helpers import auth_headers


def _send(client, headers, message, conversation_id=None):
    resp = client.post(
        "/api/v1/agent/chat",
        json={"conversation_id": conversation_id, "message": message},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _read(client, headers, conversation_id):
    resp = client.get(f"/api/v1/agent/conversation/{conversation_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _run_worker(session, *, rounds: int = 3) -> None:
    """把待办任务跑完。多跑几轮是因为「每周扫描」「推送」这些计划任务也在同一个队列里。"""
    from app.workers import runner

    for _ in range(rounds):
        summary = runner.run_once(session)
        if summary.claimed == 0:
            break


def _seed_pair(session, user_id: str) -> tuple[str, str]:
    """造两条知识条目，返回它们的 id。"""
    repo = KnowledgeRepository(session, user_id=user_id)
    with session.begin_nested():
        a = repo.create(user_id=user_id, title="专注优先", content="多任务并行是效率的关键。")
        b = repo.create(user_id=user_id, title="专注至上", content="多任务并行必然降低效率。")
    session.flush()
    return a.id, b.id


def _seed_conflict(session, user_id: str, *, suffix: str = "1") -> Conflict:
    item_a, item_b = _seed_pair(session, user_id)
    conflict = ConflictRepository(session, user_id=user_id).create(
        user_id=user_id, item_a_id=item_a, item_b_id=item_b,
        claim_a_id=f"claim-a-{suffix}", claim_b_id=f"claim-b-{suffix}",
        conflict_type="立场对立", detail="一边要多任务，一边说必须单任务",
        suggestion="挑一个场景实测再定", confidence=0.82,
    )
    session.commit()
    return conflict


def _card_of(session, conversation_id: str) -> dict | None:
    conversation = session.get(Conversation, conversation_id)
    assert conversation is not None
    return (conversation.messages or [])[-1].get("card")


# ---- 异步回合 --------------------------------------------------------------


def test_heavy_capability_is_queued_when_worker_enabled(client, session, monkeypatch):
    """worker 开着时，重能力只发一张 pending 卡并排队，不在请求里空等。"""
    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "turns_pending")

    body = _send(client, headers, "扫描知识冲突")

    assert body["capability"] == "l2"
    card = body["card"]
    assert card["kind"] == "pending"
    assert card["key"] == card["turn_id"] and card["turn_id"]
    assert card["capability"] == "l2"

    session.flush()
    queued = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == turns.task_key(card["turn_id"]))
    ).first()
    assert queued is not None, "没有入队，后台永远不会跑"
    assert queued.task_name == turns.TASK_NAME
    assert queued.payload["conversation_id"] == body["conversation_id"]


def test_worker_writes_result_back_into_the_pending_message(client, session, monkeypatch):
    """worker 执行完要把结果**就地**写回那条 pending 消息。

    写不回去的话，用户看到的就是一条永远「正在扫描」的消息——比同步等待更糟，
    因为连「它失败了」都无从知道。
    """
    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "turns_finish")

    body = _send(client, headers, "扫描知识冲突")
    turn_id = body["card"]["turn_id"]
    _run_worker(session)

    conversation = _read(client, headers, body["conversation_id"])
    last = conversation["messages"][-1]
    assert last["card"]["kind"] == "l2_conflicts"
    assert last["card"]["key"] == turn_id
    # 消息正文也从「正在扫描」换成了真实结论
    assert last["content"] != body["reply"]
    assert "扫了" in last["content"]


def test_without_worker_it_falls_back_to_sync(client, session):
    """没有 worker 时退回同步执行：慢，但一定有结果（不会留一张永远转圈的卡）。"""
    headers = auth_headers(client, "turns_sync")

    body = _send(client, headers, "扫描知识冲突")

    assert body["card"]["kind"] == "l2_conflicts"
    session.flush()
    assert session.query(TaskRun).filter(TaskRun.task_name == turns.TASK_NAME).count() == 0


def test_dead_task_becomes_a_failed_card(client, session, monkeypatch):
    """任务进了死信，pending 卡必须变成失败卡——不能让用户一直等。"""
    from app.workers.tasks import enqueue

    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "turns_dead")
    body = _send(client, headers, "扫描知识冲突")
    turn_id = body["card"]["turn_id"]

    session.flush()
    row = session.scalars(
        select(TaskRun).where(TaskRun.idempotency_key == turns.task_key(turn_id))
    ).first()
    row.status = "dead"
    row.last_error = "boom"
    session.commit()

    conversation = _read(client, headers, body["conversation_id"])
    card = conversation["messages"][-1]["card"]
    assert card["kind"] == "failed"
    assert card["key"] == turn_id
    assert card["href"] == "/conflicts.html"      # 给出原页面作为重试落点


def test_running_task_still_reads_as_pending(client, session, monkeypatch):
    """任务还在排队/执行时不能被误判成失败。"""
    monkeypatch.setattr(settings, "worker_enabled", True)
    headers = auth_headers(client, "turns_still")
    body = _send(client, headers, "扫描知识冲突")

    conversation = _read(client, headers, body["conversation_id"])
    assert conversation["messages"][-1]["card"]["kind"] == "pending"


def test_worker_disabled_yet_async_capability_still_works(client, session, monkeypatch):
    """再确认一次同步兜底不是「碰巧」：显式关掉 worker 后 L5 也要出结果。"""
    monkeypatch.setattr(settings, "worker_enabled", False)
    headers = auth_headers(client, "turns_sync_l5")
    client.post(
        "/api/v1/knowledge/items",
        json={"title": "索引", "content": "B+树更适合范围查询"},
        headers=headers,
    )

    body = _send(client, headers, "诊断我的学习状态")

    assert body["capability"] == "l5"
    assert body["card"]["kind"] == "l5_diagnosis"


# ---- 读取会话（轮询 + 历史恢复） -------------------------------------------


def test_conversation_read_restores_history_with_cards(client):
    """刷新页面要能把历史连卡片一起恢复——否则整条线程一刷新就空了。"""
    headers = auth_headers(client, "turns_hist")
    body = _send(client, headers, "记一下：B+树更适合范围查询 #数据库")

    conversation = _read(client, headers, body["conversation_id"])

    assert [m["role"] for m in conversation["messages"]] == ["user", "assistant"]
    assert conversation["messages"][-1]["card"]["kind"] == "knowledge_created"
    assert conversation["messages"][-1]["source"] == "agent"


def test_conversation_read_rejects_foreign_and_unknown(client):
    owner = auth_headers(client, "turns_owner")
    cid = _send(client, owner, "记一下：随便一条内容")["conversation_id"]

    intruder = auth_headers(client, "turns_intruder")
    assert client.get(f"/api/v1/agent/conversation/{cid}", headers=intruder).status_code == 403
    assert client.get("/api/v1/agent/conversation/not-a-real-id", headers=owner).status_code == 404
    assert client.get(f"/api/v1/agent/conversation/{cid}").status_code == 401


# ---- 操作回流 --------------------------------------------------------------


def _attach_conflict_card(session, user_id: str, conflict_ids: list[str]) -> tuple[str, str]:
    """造一个「会话里已经有一张冲突卡」的状态，返回 (会话 id, 卡片 key)。

    真实路径下这张卡是 worker 写回来的；这里直接构造，是为了把「卡片内操作」
    与「L2 扫描能不能跑出冲突」解耦——后者依赖模型输出，不该影响这条测试。
    """
    repo = ConversationRepository(session, user_id=user_id)
    conversation = repo.create(user_id=user_id)
    key = "card-key-1"
    card = turns.l2_conflicts_card(
        key=key,
        items=turns.conflict_views(session, user_id=user_id, conflict_ids=conflict_ids),
        summary={"conflicts_found": len(conflict_ids)},
    )
    repo.append_message(conversation, "user", "扫描知识冲突", source=turns.SOURCE)
    repo.append_message(conversation, "assistant", "发现冲突", source=turns.SOURCE, card=card)
    session.commit()
    return conversation.id, key


def _post_action(client, headers, payload):
    return client.post("/api/v1/agent/actions", json=payload, headers=headers)


def test_action_updates_state_and_returns_refreshed_card(client, session):
    from tests.helpers import sign_in

    user = sign_in(client, "turns_action")
    conflict = _seed_conflict(session, user.user_id, suffix="act")
    cid, key = _attach_conflict_card(session, user.user_id, [conflict.id])

    resp = _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l2.conflict.state", "target_id": conflict.id, "value": "ignored",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["card"]["kind"] == "l2_conflicts"
    assert body["card"]["items"][0]["user_state"] == "ignored"
    assert "忽略" in body["reply"]

    # 库里也是真的改了（不是只改了回包）
    session.expire_all()
    assert ConflictRepository(session, user_id=user.user_id).get(conflict.id).user_state == "ignored"


def test_action_does_not_append_new_messages(client, session):
    """采纳/忽略是对已有结果的处置，不是新的一轮对话——不该往消息流里刷噪声。"""
    from tests.helpers import sign_in

    user = sign_in(client, "turns_no_spam")
    conflict = _seed_conflict(session, user.user_id, suffix="nospam")
    cid, key = _attach_conflict_card(session, user.user_id, [conflict.id])
    before = len(session.get(Conversation, cid).messages)

    _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l2.conflict.state", "target_id": conflict.id, "value": "accepted",
    })

    session.expire_all()
    assert len(session.get(Conversation, cid).messages) == before


def test_state_survives_a_reload(client, session):
    """读会话时要按数据库刷新卡片状态。

    否则「忽略过的冲突」重新打开页面又变回待处理——卡片在说谎，
    而用户会以为自己刚才那一下没生效。
    """
    from tests.helpers import sign_in

    user = sign_in(client, "turns_reload")
    conflict = _seed_conflict(session, user.user_id, suffix="reload")
    cid, key = _attach_conflict_card(session, user.user_id, [conflict.id])
    _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l2.conflict.state", "target_id": conflict.id, "value": "ignored",
    })

    conversation = _read(client, user.headers, cid)
    assert conversation["messages"][-1]["card"]["items"][0]["user_state"] == "ignored"


def test_action_rejects_missing_card_and_unknown_action(client, session):
    from tests.helpers import sign_in

    user = sign_in(client, "turns_badact")
    conflict = _seed_conflict(session, user.user_id, suffix="bad")
    cid, key = _attach_conflict_card(session, user.user_id, [conflict.id])

    missing = _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": "not-a-key",
        "action": "l2.conflict.state", "target_id": conflict.id, "value": "ignored",
    })
    assert missing.status_code == 422
    assert "不存在" in missing.json()["detail"]

    wrong = _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l5.diagnosis.decide", "target_id": conflict.id, "value": "accepted",
    })
    assert wrong.status_code == 422
    assert "不支持" in wrong.json()["detail"]


def test_action_on_a_gone_conflict_is_rejected(client, session):
    from tests.helpers import sign_in

    user = sign_in(client, "turns_gone")
    conflict = _seed_conflict(session, user.user_id, suffix="gone")
    cid, key = _attach_conflict_card(session, user.user_id, [conflict.id])

    resp = _post_action(client, user.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l2.conflict.state", "target_id": "ghost-conflict", "value": "ignored",
    })
    assert resp.status_code == 422
    assert "不在了" in resp.json()["detail"]


def test_action_cannot_touch_someone_elses_conversation(client, session):
    from tests.helpers import sign_in

    owner = sign_in(client, "turns_act_owner")
    conflict = _seed_conflict(session, owner.user_id, suffix="own")
    cid, key = _attach_conflict_card(session, owner.user_id, [conflict.id])

    intruder = sign_in(client, "turns_act_intruder")
    resp = _post_action(client, intruder.headers, {
        "conversation_id": cid, "card_key": key,
        "action": "l2.conflict.state", "target_id": conflict.id, "value": "accepted",
    })
    assert resp.status_code == 422
    assert "无权" in resp.json()["detail"]


def test_l5_diagnosis_action_decides_and_refreshes(client, session):
    from tests.helpers import sign_in

    user = sign_in(client, "turns_l5_action")
    repo = CognitiveDiagnosisRepository(session, user_id=user.user_id)
    diagnosis = repo.create(
        user_id=user.user_id, pattern="高收藏低完成", root_cause="启动门槛过高",
        confidence=0.66, suggested_action="先把单次任务压到 15 分钟",
        reasoning_chain=["收藏 12 条", "读完 1 条"],
    )
    session.commit()

    conversation = ConversationRepository(session, user_id=user.user_id).create(user_id=user.user_id)
    key = "l5-key-1"
    from app.agent.cards import l5_diagnosis_card
    from app.agent.l5_orchestrator import L5Result, BehaviorMetrics

    card = l5_diagnosis_card(key=key, result=L5Result(
        state="ok", diagnosis_id=diagnosis.id, pattern=diagnosis.pattern,
        root_cause=diagnosis.root_cause, confidence=diagnosis.confidence,
        suggested_action=diagnosis.suggested_action,
        reasoning_chain=list(diagnosis.reasoning_chain), metrics=BehaviorMetrics(total_items=12),
    ))
    repo_conv = ConversationRepository(session, user_id=user.user_id)
    repo_conv.append_message(conversation, "user", "诊断我的学习状态", source=turns.SOURCE)
    repo_conv.append_message(conversation, "assistant", "诊断完成", source=turns.SOURCE, card=card)
    session.commit()

    resp = _post_action(client, user.headers, {
        "conversation_id": conversation.id, "card_key": key,
        "action": "l5.diagnosis.decide", "target_id": diagnosis.id, "value": "accepted",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["card"]["status"] == "accepted"

    session.expire_all()
    assert session.get(CognitiveDiagnosis, diagnosis.id).status == "accepted"


def test_weread_is_no_longer_a_guide_card(client):
    """微信读书接入后不该再回引导卡——漏改 WIRED 的表现是「点了没反应」。"""
    headers = auth_headers(client, "turns_weread_wired")
    body = _send(client, headers, "同步微信读书")
    assert body["capability"] == "weread_sync"
    assert body["card"]["kind"] != "guide"


def test_books_path_writes_nothing(client, session):
    """书架是只读能力：问一次书架不该产生任何写入。"""
    headers = auth_headers(client, "turns_books_readonly")
    _send(client, headers, "我的书架里有什么")
    session.flush()
    assert session.query(Book).count() == 0
    assert session.query(KnowledgeItem).count() == 0
